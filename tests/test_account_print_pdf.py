"""Tests for accounting voucher PDF creation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from account_print_pdf import fetch_print_page, find_purchase, save_purchase_pdf
from account_purchase_list import PurchaseRow, purchase_print_url


ROW = PurchaseRow("111540300202", "1151017", "用途", "1,280", "未審核")
PDF = b"%PDF-1.7\n" + b"x" * 120 + b"\n%%EOF\n"


class PrintPdfTests(unittest.TestCase):
    def test_fetch_print_page_recognizes_pdf_and_html(self):
        url = purchase_print_url(ROW, 115)

        class Response:
            status = 200

            def __init__(self, body, content_type):
                self.body = body
                self.headers = {"Content-Type": content_type}

            def geturl(self):
                return url

            def read(self, size):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            response = None

            def open(self, request, timeout):
                self.request = request
                return self.response

        opener = Opener()
        with patch("account_print_pdf.urllib.request.build_opener", return_value=opener):
            opener.response = Response(PDF, "application/pdf")
            self.assertEqual(fetch_print_page({}, object(), url), (PDF, "pdf"))
            self.assertEqual(opener.request.get_method(), "GET")
            opener.response = Response(b"<html><body>print</body></html>", "text/html")
            self.assertEqual(fetch_print_page({}, object(), url)[1], "html")
            opener.response = Response(b"login", "text/html")
            with self.assertRaisesRegex(ValueError, "不是可辨識"):
                fetch_print_page({}, object(), url)

    def test_find_only_selected_number(self):
        self.assertEqual(find_purchase([ROW], ROW.number), ROW)
        with self.assertRaisesRegex(ValueError, "找不到唯一"):
            find_purchase([ROW], "111540300201")
        with self.assertRaisesRegex(ValueError, "單號"):
            find_purchase([ROW], "../bad")

    def test_direct_pdf_saves_one_private_file_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "prints"
            with patch("account_print_pdf.fetch_print_page", return_value=(PDF, "pdf")) as fetch, \
                 patch("account_print_pdf.render_html_pdf") as render:
                path = save_purchase_pdf(ROW, 115, {}, object(), folder)
                self.assertEqual(path.read_bytes(), PDF)
                self.assertEqual(path.name, "憑證單_111540300202.pdf")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                fetch.assert_called_once()
                render.assert_not_called()
                with self.assertRaisesRegex(ValueError, "已存在"):
                    save_purchase_pdf(ROW, 115, {}, object(), folder)
                self.assertEqual(path.read_bytes(), PDF)

    def test_html_uses_renderer_and_rejects_non_pdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "prints"
            with patch("account_print_pdf.fetch_print_page", return_value=(b"<html></html>", "html")), \
                 patch("account_print_pdf.render_html_pdf", return_value=PDF) as render:
                path = save_purchase_pdf(ROW, 115, {}, object(), folder)
                self.assertTrue(path.exists())
                render.assert_called_once()
            other = PurchaseRow("111540300201", "1151017", "", "1", "未審核")
            with patch("account_print_pdf.fetch_print_page", return_value=(b"<html></html>", "html")), \
                 patch("account_print_pdf.render_html_pdf", return_value=b"login"):
                with self.assertRaisesRegex(ValueError, "不是完整 PDF"):
                    save_purchase_pdf(other, 115, {}, object(), folder)
                self.assertFalse((folder / "憑證單_111540300201.pdf").exists())


if __name__ == "__main__":
    unittest.main()
