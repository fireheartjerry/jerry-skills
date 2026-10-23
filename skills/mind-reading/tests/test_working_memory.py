import copy
import datetime
import json
import os
import stat
import threading
import unittest
from unittest.mock import patch

import helpers
from mr import intent_context
from mr import working_memory as wm

T0 = "2030-01-01T00:00:00Z"
T1 = "2030-01-01T00:00:10Z"
T2 = "2030-01-01T00:00:20Z"


def rec(kind="fact", text="the harbor index is rebuilt nightly", project="atlas", **extra):
    out = {"kind": kind, "text": text, "source_ref": "note:1", "project": project}
    out.update(extra)
    return out


class WorkingMemoryTests(unittest.TestCase):
    def test_windows_lock_backend_can_write_and_release(self):
        class WindowsLock:
            LK_LOCK=1
            LK_UNLCK=2
            def __init__(self):self.calls=[]
            def locking(self,fd,mode,size):self.calls.append((mode,size))
        backend=WindowsLock()
        with patch.object(wm,'fcntl',None),patch.object(wm,'msvcrt',backend,create=True):
            result=self.add(text='portable note')
        self.assertEqual(result['text'],'portable note')
        self.assertEqual(backend.calls,[(backend.LK_LOCK,1),(backend.LK_UNLCK,1)])
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.home = self.tmp / "home"
        self.state = self.home / "context" / "notebook.json"

    def add(self, now=T0, **kw):
        return wm.remember(self.home, rec(**kw), now=now)

    def test_missing_is_empty_and_private_modes(self):
        self.assertEqual(wm.load(self.home), {"version": 1, "revision": 0, "records": [], "history": []})
        self.assertFalse(self.home.exists())
        r = self.add()
        self.assertEqual(r["confidence"], "G")
        self.assertEqual(r["created_at"], T0)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        self.assertEqual(wm.load(self.home)["revision"], 1)

    def test_isolation_between_projects_and_globals(self):
        g = self.add(project="", text="global note")
        a = self.add(project="atlas", text="atlas note")
        b = self.add(project="beacon", text="beacon note")
        ids = lambda recs: [r["id"] for r in recs]
        self.assertEqual(ids(wm.query(self.home, "atlas", now=T0)), [g["id"], a["id"]])
        self.assertEqual(ids(wm.query(self.home, "beacon", now=T0)), [g["id"], b["id"]])
        self.assertEqual(ids(wm.query(self.home, None, now=T0)), [g["id"]])
        self.assertEqual(ids(wm.query(self.home, "atl", now=T0)), [g["id"]])
        self.assertNotIn("beacon note", json.dumps(wm.frame(self.home, "atlas", now=T0)))

    def test_goal_supersession_same_scope_only(self):
        g1 = self.add(kind="goal", text="ship v1")
        other = self.add(kind="goal", text="other project goal", project="beacon")
        glob = self.add(kind="goal", text="global goal", project="")
        g2 = self.add(now=T1, kind="goal", text="ship v2", phase="review")
        by_id = {r["id"]: r for r in wm.query(self.home, "atlas", now=T1, include_inactive=True)}
        self.assertEqual(by_id[g1["id"]]["status"], "superseded")
        self.assertEqual(by_id[g1["id"]]["superseded_by"], g2["id"])
        self.assertEqual(by_id[glob["id"]]["status"], "active")
        self.assertIn(other["id"], [r["id"] for r in wm.query(self.home, "beacon", now=T1)])
        self.assertEqual(wm.frame(self.home, "atlas", now=T1)["goal"], "ship v2")
        self.assertEqual(wm.frame(self.home, "atlas", now=T1)["phase"], "review")
        with self.assertRaises(wm.MemoryError):
            wm.change(self.home, g1["id"], "active", now=T2)

    def test_forgetting_newest_goal_does_not_resurrect_old_goal(self):
        g1 = self.add(kind="goal", text="ship v1")
        g2 = self.add(now=T1, kind="goal", text="ship v2")
        wm.change(self.home, g2["id"], "forgotten", now=T2)
        self.assertNotIn("goal", wm.frame(self.home, "atlas", now=T2))
        wm.change(self.home, g2["id"], "active", now=T2)
        self.assertEqual(wm.frame(self.home, "atlas", now=T2)["goal"], "ship v2")
        self.assertNotEqual(g1["id"], g2["id"])

    def test_exact_project_goal_beats_newer_global_goal(self):
        self.add(kind="goal", text="project goal")
        self.add(now=T1, kind="goal", text="global goal", project="")
        self.assertEqual(wm.frame(self.home, "atlas", now=T1)["goal"], "project goal")
        self.assertEqual(wm.frame(self.home, None, now=T1)["goal"], "global goal")

    def test_uncertainty_never_becomes_verified_fact(self):
        self.add(kind="assumption", text="cache is warm", confidence="S")
        self.add(kind="fact", text="guessed fact")  # default G
        self.add(kind="fact", text="stated fact", confidence="S")
        self.add(kind="constraint", text="no network", confidence="L")
        self.add(kind="preference", text="terse", confidence="S")
        a = self.add(kind="assumption", text="resolved guess", confidence="S")
        wm.resolve(self.home, a["id"], "checked it", "log:9", now=T1)
        facts = wm.frame(self.home, "atlas", now=T1)["verified_facts"]
        self.assertEqual(facts, ["fact: stated fact"])
        self.assertNotIn("constraint: no network", facts)
        self.assertEqual(wm.query(self.home, "atlas", now=T1, include_inactive=True)[-1]["kind"], "assumption")

    def test_frame_is_normalizable_bounded_and_has_no_future_fields(self):
        for k in range(12):
            self.add(text="fact number %d" % k, confidence="S", key="k%d" % k, value=k)
        frame = wm.frame(self.home, "atlas", now=T1)
        self.assertEqual(intent_context.normalize(frame), frame)
        self.assertEqual(len(frame["verified_facts"]), 8)
        self.assertEqual(frame["verified_facts"][0], "fact: fact number 11 [k11=11]")
        self.assertEqual(set(frame), {"project", "verified_facts"})

    def test_decisions_answers_bindings_enter_frame_when_confirmed(self):
        self.add(kind="decision", text="use sqlite", confidence="S")
        self.add(kind="answer", text="yes, staging only", confidence="S")
        self.add(kind="binding", text="'the doc' means spec.md", confidence="S", key="the doc", value="spec.md")
        facts = wm.frame(self.home, "atlas", now=T0)["verified_facts"]
        self.assertEqual(len(facts), 3)
        self.assertIn("decision: use sqlite", facts)
        self.assertIn("binding: 'the doc' means spec.md [the doc=\"spec.md\"]", facts)

    def test_expiry_exact_boundary(self):
        r = self.add(kind="fact", confidence="S", expires_at=T1)
        before = "2030-01-01T00:00:09Z"
        self.assertEqual(len(wm.query(self.home, "atlas", now=before)), 1)
        self.assertEqual(wm.query(self.home, "atlas", now=T1), [])
        self.assertEqual(wm.query(self.home, "atlas", now=T1, include_inactive=True)[0]["id"], r["id"])
        self.assertNotIn("verified_facts", wm.frame(self.home, "atlas", now=T1))

    def test_now_accepts_aware_datetime_only(self):
        self.add(expires_at=T1)
        aware = datetime.datetime(2030, 1, 1, 0, 0, 10, tzinfo=datetime.timezone.utc)
        self.assertEqual(wm.query(self.home, "atlas", now=aware), [])
        with self.assertRaises(wm.MemoryError):
            wm.query(self.home, "atlas", now=datetime.datetime(2030, 1, 1))
        with self.assertRaises(wm.MemoryError):
            wm.query(self.home, "atlas", now="2030-13-01T00:00:00Z")

    def test_forget_and_undo(self):
        r = self.add()
        f = wm.change(self.home, r["id"], "forgotten", now=T1)
        self.assertEqual(f["status"], "forgotten")
        self.assertEqual(wm.query(self.home, "atlas", now=T1), [])
        self.assertEqual(len(wm.query(self.home, "atlas", now=T1, include_inactive=True)), 1)
        back = wm.change(self.home, r["id"], "active", now=T2)
        self.assertEqual(back["status"], "active")
        self.assertEqual(len(wm.query(self.home, "atlas", now=T2)), 1)
        rev = wm.load(self.home)["revision"]
        wm.change(self.home, r["id"], "active", now=T2)  # no-op leaves revision alone
        self.assertEqual(wm.load(self.home)["revision"], rev)
        with self.assertRaises(wm.MemoryError):
            wm.change(self.home, r["id"], "superseded")
        with self.assertRaises(wm.MemoryError):
            wm.change(self.home, "missing", "closed")

    def test_resolve_marks_resolution_without_promoting(self):
        a = self.add(kind="assumption", text="api is stable")
        out = wm.resolve(self.home, a["id"], "vendor confirmed", "mail:7", now=T1)
        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["kind"], "assumption")
        self.assertEqual(out["resolution"], {"text": "vendor confirmed", "source_ref": "mail:7", "at": T1})
        self.assertEqual([r for r in wm.query(self.home, "atlas", now=T1) if r["kind"] == "fact"], [])
        for bad in ((self.add(kind="fact")["id"], "x", "y"), (a["id"], "again", "z"), (self.add(kind="loop")["id"], "", "z"),
                    (self.add(kind="loop")["id"], "ok", " ")):
            with self.assertRaises(wm.MemoryError):
                wm.resolve(self.home, *bad)
        with self.assertRaises(wm.MemoryError):
            wm.change(self.home, a["id"], "active")

    def test_ready_blocked_dependencies(self):
        base = self.add(kind="loop", id="base", text="base")
        mid = self.add(kind="loop", id="mid", text="mid", requires=["base"])
        self.add(kind="loop", id="leaf", text="leaf", requires=["mid", "gone"])
        self.add(kind="loop", id="free", text="free")
        r = wm.ready(self.home, "atlas", now=T0)
        self.assertEqual([x["id"] for x in r["ready"]], ["base", "free"])
        reasons = {b["record"]["id"]: {(x["id"], x["reason"]) for x in b["reasons"]} for b in r["blocked"]}
        self.assertEqual(reasons["mid"], {("base", "open")})
        self.assertEqual(reasons["leaf"], {("mid", "open"), ("gone", "missing")})
        wm.change(self.home, base["id"], "closed", now=T1)
        r = wm.ready(self.home, "atlas", now=T1)
        self.assertEqual([x["id"] for x in r["ready"]], ["mid", "free"])
        wm.resolve(self.home, "mid", "done by hand", "log:1", now=T1)
        self.assertIn("leaf", [b["record"]["id"] for b in wm.ready(self.home, "atlas", now=T1)["blocked"]])
        self.add(kind="loop", id="late", text="late", requires=["base"])
        wm.change(self.home, "base", "forgotten", now=T2)
        self.assertEqual({x["reason"] for b in wm.ready(self.home, "atlas", now=T2)["blocked"]
                          if b["record"]["id"] == "late" for x in b["reasons"]}, {"forgotten"})

    def test_ready_expired_dependency_blocks_at_boundary(self):
        self.add(kind="loop", id="dep", text="dep", expires_at=T1)
        self.add(kind="loop", id="top", text="top", requires=["dep"])
        wm.change(self.home, "dep", "closed", now=T0)
        self.assertEqual("top", wm.ready(self.home, "atlas", now="2030-01-01T00:00:09Z")["ready"][0]["id"])
        blocked = wm.ready(self.home, "atlas", now=T1)["blocked"]
        self.assertEqual(blocked[0]["reasons"], [{"id": "dep", "reason": "expired"}])

    def test_cycles_blocked_and_reported(self):
        self.add(kind="loop", id="a", text="a", requires=["b"])
        self.add(kind="loop", id="b", text="b", requires=["c"])
        self.add(kind="loop", id="c", text="c", requires=["a"])
        self.add(kind="loop", id="d", text="d", requires=["a"])
        self.add(kind="loop", id="self", text="self", requires=["self"])
        r = wm.ready(self.home, "atlas", now=T0)
        self.assertEqual(r["ready"], [])
        by_id = {b["record"]["id"]: b for b in r["blocked"]}
        self.assertEqual(by_id["a"]["cycle"], ["a", "b", "c"])
        self.assertEqual(by_id["self"]["cycle"], ["self"])
        self.assertNotIn("cycle", by_id["d"])
        self.assertEqual(by_id["d"]["reasons"], [{"id": "a", "reason": "open"}])

    def test_cross_project_dependencies_do_not_leak(self):
        self.add(kind="loop", id="secret", text="secret", project="beacon")
        wm.change(self.home, "secret", "closed", now=T0)
        self.add(kind="loop", id="mine", text="mine", requires=["secret"])
        glob = self.add(kind="loop", id="shared", text="shared", project="")
        wm.change(self.home, "shared", "closed", now=T0)
        self.add(kind="loop", id="viaglobal", text="v", requires=["shared"])
        r = wm.ready(self.home, "atlas", now=T0)
        self.assertEqual([x["id"] for x in r["ready"]], ["viaglobal"])
        self.assertEqual(r["blocked"][0]["reasons"], [{"id": "secret", "reason": "missing"}])
        self.assertNotIn("secret", json.dumps(wm.ready(self.home, None, now=T0)).replace('"id": "secret", "reason"', ""))
        self.assertNotIn("beacon", json.dumps(r))
        self.assertEqual(glob["project"], "")

    def test_history_revisions_and_since(self):
        g1 = self.add(kind="goal", text="one")
        self.add(now=T1, kind="goal", text="two")
        events = wm.history(self.home)
        self.assertEqual([(e["rev"], e["op"]) for e in events], [(1, "remember"), (2, "remember"), (2, "supersede")])
        sup = events[-1]
        self.assertEqual(sup["before"]["status"], "active")
        self.assertEqual(sup["after"]["status"], "superseded")
        self.assertEqual(set(events[0]), {"rev", "op", "id", "before", "after", "time"})
        self.assertIsNone(events[0]["before"])
        self.assertEqual([e["op"] for e in wm.history(self.home, since=1)], ["remember", "supersede"])
        self.assertEqual(wm.history(self.home, since=2), [])
        with self.assertRaises(wm.MemoryError):
            wm.history(self.home, since=-1)
        with self.assertRaises(wm.MemoryError):
            wm.history(self.home, since=True)

    def test_history_bounded_and_record_limit(self):
        r = self.add()
        state = wm.load(self.home)
        state["records"] = [dict(r, id="r%d" % k, seq=1) for k in range(wm.MAX_RECORDS)]
        state["history"] = [{"rev": 1, "op": "x", "id": "r0", "before": None, "after": None, "time": T0}] * 2001
        with self.assertRaises(wm.MemoryError):
            wm._validate_state(state)
        state["history"] = []
        wm._validate_state(state)
        state["records"].append(dict(r, id="extra", seq=1))
        with self.assertRaises(wm.MemoryError):
            wm._validate_state(state)

    def test_history_trims_oldest_events(self):
        r = self.add()
        state = wm.load(self.home)
        state["history"] = [{"rev": 1, "op": "x", "id": r["id"], "before": None, "after": None, "time": T0}] * wm.MAX_HISTORY
        self.state.write_text(json.dumps(state))
        wm.change(self.home, r["id"], "closed", now=T1)
        events = wm.history(self.home)
        self.assertEqual(len(events), wm.MAX_HISTORY)
        self.assertEqual(events[-1]["op"], "change")
        self.assertEqual(events[0]["op"], "x")

    def test_history_has_no_text_beyond_explicit_record(self):
        self.add(text="explicit note")
        wm.change(self.home, wm.query(self.home, "atlas", now=T0)[0]["id"], "closed")
        blob = json.dumps(wm.history(self.home))
        self.assertIn("explicit note", blob)
        self.assertNotIn("current_request", blob)

    def test_record_validation_changes_nothing(self):
        self.add()
        raw = self.state.read_bytes()
        bad = [
            rec(kind="mystery"), rec(text=""), rec(text=" "), rec(text="x" * 2001), rec(text=5),
            {k: v for k, v in rec().items() if k != "source_ref"},
            {k: v for k, v in rec().items() if k != "project"},
            rec(project=None), rec(unknown="x"), rec(confidence="X"), rec(confidence="s"),
            rec(status="forgotten"), rec(status="superseded"), rec(id="bad id"), rec(id=7), rec(id="../x"),
            rec(expires_at="2030-01-01"), rec(expires_at="2030-02-30T00:00:00Z"), rec(expires_at=5),
            rec(requires="a"), rec(requires=["a", "a"]), rec(requires=["bad id"]), rec(terms=["x"] * 21),
            rec(terms=[1]), rec(value=object()), rec(value={"a": float("nan")}), rec(value=["x" * 1001]),
            rec(value=[[[[[[1]]]]]]), rec(value="x" * 1001), rec(value={1: 2}), rec(key="k" * 201),
            rec(phase="p" * 81), rec(text="a\x00b"), rec(seq=3), rec(created_at=T0), "record", None,
            {"kind": "fact", 1: 2, "text": "x", "source_ref": "s", "project": ""},
        ]
        for item in bad:
            with self.assertRaises(wm.MemoryError, msg=repr(item)[:80]):
                wm.remember(self.home, item, now=T0)
        for args in ((None,), ("a/b",), (7,)):
            with self.assertRaises(wm.MemoryError):
                wm.change(self.home, args[0], "closed")
        with self.assertRaises(wm.MemoryError):
            wm.change(self.home, self.add(now=T0, id="dup")["id"], "bogus")
        with self.assertRaises(wm.MemoryError):
            self.add(id="dup")
        with self.assertRaises(wm.MemoryError):
            wm.query(self.home, project=3)
        self.assertEqual(len(wm.load(self.home)["records"]), 2)
        self.assertNotEqual(raw, b"")

    def test_known_json_extras_round_trip(self):
        r = self.add(kind="binding", key="alias", value={"a": [1, 2.5, None, True, "s"]}, terms=["t"],
                     unless=["u"], alternatives=["x", "y"], counter_to=["mem_old"], condition={"terms":["c"]},
                     expires_at="2031-01-01T00:00:00Z", confidence="L", phase="draft", id="keep.me-1")
        got = wm.query(self.home, "atlas", now=T0)[0]
        self.assertEqual(got, r)
        self.assertEqual(got["id"], "keep.me-1")
        self.assertEqual(got["value"], {"a": [1, 2.5, None, True, "s"]})

    def test_corrupt_state_is_preserved(self):
        self.add()
        good = json.loads(self.state.read_text())
        cases = [b"{not json", b"[]", json.dumps(dict(good, version=2)).encode(),
                 json.dumps(dict(good, revision=-1)).encode(), json.dumps(dict(good, extra=1)).encode(),
                 json.dumps(dict(good, records=[dict(good["records"][0], status="weird")])).encode(),
                 json.dumps(dict(good, records=good["records"] * 2)).encode(),
                 json.dumps(dict(good, history=[{"rev": 1}])).encode()]
        for blob in cases:
            self.state.write_bytes(blob)
            for call in (lambda: wm.load(self.home), lambda: wm.remember(self.home, rec(), now=T0),
                         lambda: wm.change(self.home, "x", "closed"), lambda: wm.query(self.home)):
                with self.assertRaises(wm.MemoryError):
                    call()
            self.assertEqual(self.state.read_bytes(), blob)

    def test_symlinked_output_ancestors_are_rejected(self):
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        with self.assertRaises(wm.MemoryError):
            wm.remember(link, rec(), now=T0)
        with self.assertRaises(wm.MemoryError):
            wm.remember(link / "nested", rec(), now=T0)
        self.assertEqual(list(real.iterdir()), [])
        self.home.mkdir()
        (self.home / "context").symlink_to(real)
        with self.assertRaises(wm.MemoryError):
            wm.remember(self.home, rec(), now=T0)
        with self.assertRaises(wm.MemoryError):
            wm.load(self.home)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_notebook_and_lock_are_rejected(self):
        self.add()
        victim = self.tmp / "victim.json"
        victim.write_text("keep")
        self.state.unlink()
        self.state.symlink_to(victim)
        with self.assertRaises(wm.MemoryError):
            wm.remember(self.home, rec(), now=T0)
        self.assertEqual(victim.read_text(), "keep")
        self.state.unlink()
        lock = self.home / "context" / ".notebook.lock"
        lock.unlink()
        lock.symlink_to(victim)
        with self.assertRaises(wm.MemoryError):
            wm.remember(self.home, rec(), now=T0)
        self.assertEqual(victim.read_text(), "keep")

    def test_no_temp_files_left_behind(self):
        self.add()
        self.add()
        names = sorted(p.name for p in (self.home / "context").iterdir())
        self.assertEqual(names, [".notebook.lock", "notebook.json"])
        self.assertEqual(stat.S_IMODE((self.home / "context" / ".notebook.lock").stat().st_mode), 0o600)

    def test_concurrent_writers_lose_no_updates(self):
        errors = []

        def worker(n):
            try:
                for k in range(6):
                    wm.remember(self.home, rec(text="w%d-%d" % (n, k)), now=T0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        state = wm.load(self.home)
        self.assertEqual(state["revision"], 48)
        self.assertEqual(len(state["records"]), 48)
        self.assertEqual(sorted(r["seq"] for r in state["records"]), list(range(1, 49)))
        self.assertEqual(len({r["text"] for r in state["records"]}), 48)
        self.assertEqual([e["rev"] for e in state["history"]], list(range(1, 49)))

    def test_load_returns_copy(self):
        self.add()
        state = wm.load(self.home)
        state["records"][0]["text"] = "mutated"
        self.assertNotEqual(wm.load(self.home)["records"][0]["text"], "mutated")
        got = wm.query(self.home, "atlas", now=T0)
        got[0]["text"] = "mutated"
        self.assertNotEqual(wm.query(self.home, "atlas", now=T0)[0]["text"], "mutated")


if __name__ == "__main__":
    unittest.main()
