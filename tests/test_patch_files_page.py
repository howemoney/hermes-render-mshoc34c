from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]

# The upstream block the patch anchors on, in a minimal but syntactically
# faithful copy of the /api/files handler.
UPSTREAM = '''\
@app.get("/api/files")
async def list_managed_files(request: Request, path: Optional[str] = None):
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        with os.scandir(target) as scan:
            entries = [
                _managed_file_entry(policy, Path(entry.path))
                for entry in scan
                if not _is_sensitive_path(Path(entry.path))
            ]
    except PermissionError:
        raise HTTPException(status_code=403, detail="Directory is not readable")
    return {"entries": entries}
'''


def load_patch_files_page():
    module_path = REPO_ROOT / "scripts" / "patch-files-page.py"
    spec = importlib.util.spec_from_file_location("patch_files_page", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_patch(module, source: str) -> tuple[int, str]:
    with TemporaryDirectory() as tmp:
        target = Path(tmp) / "web_server.py"
        target.write_text(source, encoding="utf-8")
        argv = sys.argv
        sys.argv = ["patch-files-page.py", str(target)]
        try:
            code = module.main()
        finally:
            sys.argv = argv
        return code, target.read_text(encoding="utf-8")


class PatchFilesPageTests(unittest.TestCase):
    def test_patch_applies_and_stays_valid_python(self):
        module = load_patch_files_page()

        code, patched = run_patch(module, UPSTREAM)

        self.assertEqual(code, 0)
        self.assertIn(module.MARKER, patched)
        self.assertNotIn("entries = [\n", patched)
        self.assertIn("with os.scandir(target) as scan:", patched)
        ast.parse(patched)

    def test_patched_listing_skips_only_400_and_403(self):
        module = load_patch_files_page()

        _, patched = run_patch(module, UPSTREAM)

        self.assertIn("if exc.status_code in (400, 403):", patched)
        self.assertIn("continue", patched)
        # Anything else still propagates to the caller.
        self.assertIn("raise", patched)

    def test_patch_is_idempotent(self):
        module = load_patch_files_page()

        _, once = run_patch(module, UPSTREAM)
        code, twice = run_patch(module, once)

        self.assertEqual(code, 0)
        self.assertEqual(once, twice)

    def test_missing_anchor_fails_the_build(self):
        module = load_patch_files_page()

        code, untouched = run_patch(module, "# upstream refactored this file\n")

        self.assertEqual(code, 1)
        self.assertNotIn(module.MARKER, untouched)

    def test_dockerfile_runs_the_patch(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("scripts/patch-files-page.py", dockerfile)
        self.assertIn(
            "patch-files-page.py /opt/hermes/hermes_cli/web_server.py", dockerfile
        )


if __name__ == "__main__":
    unittest.main()
