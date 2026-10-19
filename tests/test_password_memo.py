"""Tests for the local password memo."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from email.message import Message
from unittest.mock import patch

from ncu_form_app import Handler, START_CSRF, load_password_memos, update_password_memos


class FakeServer:
    server_port = 8765


class PasswordMemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "p@ssw0rd.json"
        self.path.write_text("[]\n", encoding="utf-8")
        self.path.chmod(0o600)
        patcher = patch("ncu_form_app.PASSWORD_JSON", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, path, **fields):
        body = urllib.parse.urlencode(fields).encode()
        handler = object.__new__(Handler)
        handler.path = path
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/x-www-form-urlencoded"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append(
            (status, output.decode()))
        handler.do_POST()
        return pages[0]

    def test_add_edit_reveal_delete_and_file_permissions(self):
        status, listing = self.post("/memo/add", csrf=START_CSRF,
                                    system="示例系統", account="example@example.org",
                                    password="synthetic-secret")
        self.assertEqual(status, 200)
        self.assertIn("示例系統", listing)
        self.assertIn("example@example.org", listing)
        self.assertNotIn("synthetic-secret", listing)
        self.assertNotIn("已更新 p@ssw0rd.json。", listing)
        self.assertIn('class="memo-password">', listing)
        self.assertIn('class="inline memo-reveal"', listing)
        self.assertIn('<script src="/memo.js" defer></script>', listing)
        self.assertNotIn("帳號密碼備忘錄", listing)
        self.assertNotIn("密碼以明文保存在本機", listing)
        self.assertNotIn("<h2>新增</h2>", listing)
        self.assertIn('class="memo-system"', listing)
        self.assertEqual(json.loads(self.path.read_text()),
                         [{"system": "示例系統", "account": "example@example.org",
                           "password": "synthetic-secret"}])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

        status, edit = self.post("/memo/edit", csrf=START_CSRF,
                                 originalSystem="示例系統",
                                 originalAccount="example@example.org")
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-secret", edit)
        status, listing = self.post("/memo/update", csrf=START_CSRF,
                                    originalSystem="示例系統",
                                    originalAccount="example@example.org",
                                    system="新系統", account="changed", password="")
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-secret", listing)
        self.assertEqual(load_password_memos()[0]["password"], "synthetic-secret")
        status, revealed = self.post("/memo/reveal", csrf=START_CSRF,
                                     originalSystem="新系統", originalAccount="changed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(revealed), {"password": "synthetic-secret"})
        self.assertNotIn("<html", revealed)

        status, listing = self.post("/memo/delete", csrf=START_CSRF,
                                    originalSystem="新系統", originalAccount="changed")
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-secret", listing)
        self.assertEqual(load_password_memos(), [])

    def test_rejects_duplicate_and_invalid_csrf_without_writing(self):
        update_password_memos("add", {"system": "系統甲", "account": "same", "password": "first"})
        before = self.path.read_bytes()
        status, _ = self.post("/memo/add", csrf=START_CSRF,
                              system="系統甲", account="same", password="second")
        self.assertEqual(status, 400)
        status, _ = self.post("/memo/delete", csrf="bad",
                              originalSystem="系統甲", originalAccount="same")
        self.assertEqual(status, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_file_is_not_overwritten(self):
        self.path.write_text('{"id": "wrong-shape"}', encoding="utf-8")
        before = self.path.read_bytes()
        status, _ = self.post("/memo/add", csrf=START_CSRF,
                              system="示例系統", account="safe",
                              password="synthetic-secret")
        self.assertEqual(status, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_password_is_returned_as_json_for_inline_reveal(self):
        update_password_memos("add", {"system": "示例系統", "account": "safe",
                                       "password": '<script>alert(1)</script>'})
        status, output = self.post("/memo/reveal", csrf=START_CSRF,
                                   originalSystem="示例系統", originalAccount="safe")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(output), {"password": '<script>alert(1)</script>'})
        script = (Path(__file__).parent.parent / "browser" / "memo.js").read_text(
            encoding="utf-8")
        self.assertIn("passwordCell.textContent = data.password", script)
        self.assertNotIn("innerHTML", script)

    def test_legacy_id_is_preserved_as_account_on_next_write(self):
        self.path.write_text(json.dumps([{"id": "old-account", "password": "old-secret"}]),
                             encoding="utf-8")
        self.assertEqual(load_password_memos(),
                         [{"system": "", "account": "old-account", "password": "old-secret"}])
        status, listing = self.post("/memo/update", csrf=START_CSRF,
                                    originalSystem="", originalAccount="old-account",
                                    system="舊系統", account="old-account", password="")
        self.assertEqual(status, 200)
        self.assertNotIn("old-secret", listing)
        self.assertEqual(json.loads(self.path.read_text()),
                         [{"system": "舊系統", "account": "old-account",
                           "password": "old-secret"}])


if __name__ == "__main__":
    unittest.main()
