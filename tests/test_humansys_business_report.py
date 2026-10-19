"""Tests for HumanSys business-trip report printing."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from humansys_business_report import (
    REPORT_URL, BusinessReportRow, parse_report, save_report_pdf,
)


HEAD = """<table><tr><th>申請單號</th><th>單位</th><th>開始日期</th>
<th>結束日期</th><th>本次申請時數</th><th>表單狀態</th><th>功能</th></tr>"""
ROW = """<tr><td>74643</td><td>單位</td><td>1151027</td><td>1151027</td>
<td>4</td><td>簽核完成</td><td><a target="_blank"
href="/HumanSys/personal/personalAttend/personalBusinessRecordReport_print/74643">列印</a></td></tr>"""
PRINT_URL = "https://cis.ncu.edu.tw/HumanSys/personal/personalAttend/personalBusinessRecordReport_print/74643"
PDF = b"%PDF-1.7\n" + b"x" * 120 + b"\n%%EOF\n"


class Response:
    def __init__(self, url, body, content_type="application/pdf"):
        self.url = url
        self.body = body
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return self.url

    def getcode(self):
        return 200

    def read(self, size):
        return self.body


class BusinessReportTests(unittest.TestCase):
    def test_report_row_extracts_verified_print_link(self):
        rows = parse_report((HEAD + ROW + "</table>").encode(), "text/html; charset=utf-8", REPORT_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].number, "74643")
        self.assertEqual(rows[0].values["開始日期"], "1151027")
        self.assertEqual(rows[0].print_url, PRINT_URL)

    def test_rejects_unmatched_or_offsite_print_link(self):
        for altered in (ROW.replace("74643\">列印", "74644\">列印"),
                        ROW.replace("/HumanSys/personal/personalAttend/personalBusinessRecordReport_print/74643",
                                    "https://other.example/print/74643")):
            rows = parse_report((HEAD + altered + "</table>").encode(), "text/html", REPORT_URL)
            self.assertIsNone(rows[0].print_url)

    def test_refuses_wrong_page_or_missing_table(self):
        with self.assertRaisesRegex(ValueError, "沒有到達"):
            parse_report((HEAD + ROW + "</table>").encode(), "text/html", REPORT_URL + "/other")
        with self.assertRaisesRegex(ValueError, "表格欄位"):
            parse_report(b"<html>login</html>", "text/html", REPORT_URL)

    def test_saves_one_private_pdf_and_never_overwrites(self):
        row = BusinessReportRow({"申請單號": "74643"}, PRINT_URL)
        session = SimpleNamespace(opener=object())
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "prints"
            with patch("humansys_business_report.open_page",
                       return_value=Response(PRINT_URL, PDF)) as opened:
                path = save_report_pdf(session, row, folder)
                self.assertEqual(path.name, "申請單_74643.pdf")
                self.assertEqual(path.read_bytes(), PDF)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                opened.assert_called_once()
                with self.assertRaisesRegex(ValueError, "已存在"):
                    save_report_pdf(session, row, folder)

    def test_refuses_missing_print_link(self):
        row = BusinessReportRow({"申請單號": "74643"}, None)
        with patch("humansys_business_report.open_page") as opened:
            with self.assertRaisesRegex(ValueError, "沒有可辨識"):
                save_report_pdf(SimpleNamespace(opener=object()), row)
            opened.assert_not_called()

    def test_renders_verified_html_print_page(self):
        row = BusinessReportRow({"申請單號": "74643"}, PRINT_URL)
        page = b"<html><title>NCU - \xe5\x85\xac\xe5\xb7\xae\xe7\x94\xb3\xe8\xab\x8b\xe5\x96\xae</title></html>"
        with tempfile.TemporaryDirectory() as temporary, \
             patch("humansys_business_report.open_page",
                   return_value=Response(PRINT_URL, page, "text/html")), \
             patch("humansys_business_report._human_cookies", return_value=[{"name": "x", "value": "y", "path": "/HumanSys"}]), \
             patch("humansys_business_report.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=PDF, stderr=b"")) as render:
            path = save_report_pdf(SimpleNamespace(opener=object()), row,
                                   Path(temporary) / "prints")
            self.assertEqual(path.read_bytes(), PDF)
            self.assertEqual(render.call_count, 1)

    def test_rejects_html_that_is_not_business_print_page(self):
        row = BusinessReportRow({"申請單號": "74643"}, PRINT_URL)
        with patch("humansys_business_report.open_page",
                   return_value=Response(PRINT_URL, b"<title>Login</title>", "text/html")), \
             patch("humansys_business_report.subprocess.run") as render:
            with self.assertRaisesRegex(ValueError, "不是公差申請單"):
                save_report_pdf(SimpleNamespace(opener=object()), row)
            render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
