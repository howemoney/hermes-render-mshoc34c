from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path("/opt/hermes")


def load_patcher():
    path = REPO_ROOT / "scripts" / "patch-chat-attachments.py"
    spec = importlib.util.spec_from_file_location("patch_chat_attachments", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PatchChatAttachmentsTests(unittest.TestCase):
    def test_patches_current_upstream_sources(self):
        module = load_patcher()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in module.TARGETS:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((SOURCE_ROOT / rel).read_bytes())

            argv = sys.argv
            sys.argv = ["patch-chat-attachments.py", str(root)]
            try:
                self.assertEqual(module.main(), 0)
            finally:
                sys.argv = argv

            web_server = (root / "hermes_cli/web_server.py").read_text()
            web_models = (root / "hermes_cli/web_models.py").read_text()
            chat_page = (root / "web/src/pages/ChatPage.tsx").read_text()
            upload_lib = (root / "web/src/lib/chatImagePaste.ts").read_text()
            api = (root / "web/src/lib/api.ts").read_text()

            self.assertIn(module.MARKER, web_server)
            self.assertIn("/api/chat/file-upload", web_server)
            self.assertIn("cache_document_from_bytes", web_server)
            self.assertIn("ChatFileUpload", web_models)
            self.assertIn('type="file"', chat_page)
            self.assertIn("Paperclip", chat_page)
            self.assertIn("uploadChatFile", upload_lib)
            self.assertIn("window.location.assign(body.login_url)", api)

    def test_patch_is_idempotent(self):
        module = load_patcher()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in module.TARGETS:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((SOURCE_ROOT / rel).read_bytes())
            argv = sys.argv
            try:
                sys.argv = ["patch-chat-attachments.py", str(root)]
                self.assertEqual(module.main(), 0)
                once = {rel: (root / rel).read_bytes() for rel in module.TARGETS}
                self.assertEqual(module.main(), 0)
                twice = {rel: (root / rel).read_bytes() for rel in module.TARGETS}
            finally:
                sys.argv = argv
            self.assertEqual(once, twice)

    def test_dockerfile_applies_patch_and_rebuilds_dashboard(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("scripts/patch-chat-attachments.py", dockerfile)
        self.assertIn("npm run build --workspace web", dockerfile)


if __name__ == "__main__":
    unittest.main()
