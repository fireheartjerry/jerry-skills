import json
import gzip
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mr import trace_store as store


class TapeTests(unittest.TestCase):
    def test_remote_bundle_transaction_resume_and_parser_guard(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"bundle.gz"; db=store.connect(Path(d)/"db.sqlite")
            ev=dict(id="a",source="claude",session="s",role="user",actor="human",time="2026-01-01T00:00:00Z",ordinal=0,text="one request",source_ref="r")
            receipt=dict(id="f",path="remote:fiction",source="claude",machine="fiction",size=10,sha256="x",parser="p")
            records=[{"kind":"start","file":receipt},{"kind":"event","event":ev},{"kind":"end","file_id":"f","event_count":1,"diagnostics":{}}]
            with gzip.open(path,"wt") as f:
                for row in records:f.write(json.dumps(row)+"\n")
            with self.assertRaises(ValueError):store.import_bundle(db,path,"wrong")
            self.assertEqual(store.coverage(db)["files"],0)
            self.assertEqual(store.import_bundle(db,path,"p")["imported"],1)
            self.assertEqual(store.import_bundle(db,path,"p")["cached"],1)
            self.assertEqual(store.coverage(db)["events"],1)
            db.close()

    def test_parent_compaction_and_skip_are_equivalent_to_full_event_oracle(self):
        from mr.trace_cases import build_examples
        with tempfile.TemporaryDirectory() as d:
            db=store.connect(Path(d)/"db.sqlite");events=[]
            for i,text in enumerate(["start the repair","verify all siblings","finish the work"]):
                events.append(dict(id="human%d"%i,source="claude",machine="fiction",session="root",project="p",role="user",actor="human",text=text,time="2026-01-01T00:00:0%dZ"%i,ordinal=i,source_ref="r%d"%i,parent_session=None,action=None))
            for i in range(100):
                events.append(dict(id="tool%d"%i,source="claude",machine="fiction",session="child",project="p",role="tool",actor="agent",text="",time="2026-01-01T00:00:00Z",ordinal=i,source_ref="t%d"%i,parent_session="root",action=None))
            for e in events:
                db.execute("insert into events values (?,?,?,?,?,?,?,?)",(e['id'],'claude:'+e['session'],'claude',e['role'],e['actor'],e['time'],e['ordinal'],json.dumps(e)))
            db.commit()
            self.assertEqual(store.make_examples(db),build_examples(events))
            db.close()
    def test_hash_freezes_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a"
            p.write_bytes(b"abc")
            h = store._digest(p, 3)
            p.write_bytes(b"abcdef")
            self.assertEqual(h, store._digest(p, 3))
            with self.assertRaises(ValueError):
                store._digest(p, 10)

    def test_resume_dedup_and_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            p, q = Path(d) / "one.jsonl", Path(d) / "two.jsonl"
            p.write_text("{}\n")
            q.write_text("{}\n")
            db = store.connect(Path(d) / "tape.db")
            ev = dict(id="a", source="claude", session="s", role="user", actor="human", text="hello",
                      ordinal=0, time="2024-01-01T00:00:00Z", source_ref="r", action=None)
            with patch("mr.trace_ingest.iter_events", return_value=iter([ev])):
                store.ingest_file(db, p, "claude", parser="v1")
            self.assertTrue(store.ingest_file(db, p, "claude", parser="v1")["cached"])
            with self.assertRaises(ValueError):
                store.ingest_file(db, p, "claude", parser="v2")
            ev2 = dict(ev, text="changed", source_ref="q")
            with patch("mr.trace_ingest.iter_events", return_value=iter([ev2])):
                store.ingest_file(db, q, "claude", parser="v1")
            self.assertEqual(store.coverage(db)["events"], 1)
            self.assertEqual(store.coverage(db)["native_id_conflicts"], 1)
            self.assertEqual(list(store.iter_sessions(db))[0][1][0]["text"], "hello")
            db.close()

    def test_mutation_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "one.jsonl"
            p.write_text("{}\n")
            db = store.connect(Path(d) / "tape.db")
            def corrupt(*args, **kwargs):
                p.write_text("[]\n")
                return iter([])
            with patch("mr.trace_ingest.iter_events", side_effect=corrupt):
                with self.assertRaises(ValueError):
                    store.ingest_file(db, p, "claude", parser="v1")
            self.assertEqual(store.coverage(db)["files"], 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
