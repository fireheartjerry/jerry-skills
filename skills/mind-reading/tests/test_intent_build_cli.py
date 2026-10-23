import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
import helpers

SCRIPT = Path(__file__).resolve().parents[1]/"scripts"/"intent_build.py"


class BuilderTests(unittest.TestCase):
    def test_explicit_home_is_required(self):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "prepare"], capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"--home", r.stderr)

    def test_invalid_source_does_not_read_or_create_home(self):
        root = helpers.tmpdir("intent-build-")
        import shutil
        self.addCleanup(shutil.rmtree, root, True)
        target = root/"not-created"
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--home", str(target), "ingest", "--source", "not-a-source"], capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(target.exists())
        self.assertIn(b"source must be", r.stderr)
