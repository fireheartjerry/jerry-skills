import json
from pathlib import Path
import tempfile
import unittest
import helpers
from mr import trace_legacy_agents as legacy


class LegacyTests(unittest.TestCase):
    def test_copied_human_marker_in_worker_file_is_not_new_user_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"agent-abc.jsonl"
            row={"type":"user","uuid":"original","sessionId":"parent","origin":{"kind":"human"},"timestamp":"2026-01-01T00:00:00Z","message":{"role":"user","content":"copied parent request"}}
            path.write_text(json.dumps(row)+"\n")
            ev=list(legacy.iter_events(path))
            self.assertEqual(ev[0]["actor"],"agent")
            self.assertEqual(ev[0]["parent_session"],"parent")
            self.assertEqual(ev[0]["session"],"parent/agent-abc")
            self.assertNotIn("user/human",json.loads(ev[-1]["action"]["summary"])["events"])

    def test_workflow_journal_keeps_event_shape_without_private_reasoning(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"parent"/"subagents"/"workflows"/"wf-a"/"journal.jsonl"
            path.parent.mkdir(parents=True);path.write_text(json.dumps({"type":"finished","reasoning":"hidden sentinel","exit_code":0})+"\n")
            events=list(legacy.iter_events(path))
            self.assertNotIn("hidden sentinel",json.dumps(events))
            self.assertEqual(events[0]["actor"],"agent")
            self.assertEqual(legacy.discover(Path(d)),[path])
