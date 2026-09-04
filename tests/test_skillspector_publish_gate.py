"""Regression tests for the build-time SkillSpector publish gate patch."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "scripts" / "patch-skillspector-publish-gate.py"
UPSTREAM = Path("/opt/hermes/hermes_cli/skills_hub.py")


class SkillSpectorPublishGatePatchTests(unittest.TestCase):
    @unittest.skipUnless(UPSTREAM.exists(), "upstream Hermes source unavailable")
    def test_patch_is_idempotent_and_keeps_native_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "skills_hub.py"
            target.write_bytes(UPSTREAM.read_bytes())
            first = subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            text = target.read_text(encoding="utf-8")
            self.assertIn("_HOWE_SKILLSPECTOR_PUBLISH_GATE", text)
            self.assertIn('"--fail-on-incomplete"', text)
            self.assertIn('"--no-llm"', text)
            self.assertIn('recommendation != "SAFE"', text)
            self.assertIn("artifact_sha256", text)
            self.assertIn('result = scan_skill(path, source="self")', text)
            second = subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("no-op", second.stdout)


if __name__ == "__main__":
    unittest.main()
