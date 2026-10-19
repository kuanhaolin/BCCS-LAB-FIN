"""Tests for appropriation listing and printing."""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appropriation_list import (
    AppropriationRow, LIST_URL, parse_list, save_pdf,
)


NUMBER = "115p010224"
DETAIL = f"https://cis.ncu.edu.tw/Appropriation/user/payloadlist_detail.do?serial_no={NUMBER}"
PRINT = f"https://cis.ncu.edu.tw/Appropriation/user/payloadlist_P.do?serial_no={NUMBER}"


class Response:
    def __init__(self, url, body, content_type):
        self.url, self.body = url, body
        self.headers = {"Content-Type": content_type}

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def read(self, _limit):
        return self.body


class AppropriationListTests(unittest.TestCase):
    def test_parse_list_reads_number_and_detail_link(self):
        body = f'''<table><tr><th>序號</th><th>預計撥款日期</th><th>製表日期</th>
        <th>計畫流水號</th><th>備註</th><th>刪除</th><th>修改</th>
        <th>以舊清冊新增</th><th>詳細內容</th></tr>
        <tr><td>{NUMBER}</td><td>1151030</td><td>1151017</td><td>11421012</td>
        <td>測試</td><td></td><td></td><td></td>
        <td><a href="payloadlist_detail.do?serial_no={NUMBER}">詳細內容</a></td></tr></table>
        <a href="payloadlist_Q.do?isShowAll=true&amp;page=2">2</a>'''.encode()
        rows, pages = parse_list(body, "text/html;charset=utf-8", LIST_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].number, NUMBER)
        self.assertEqual(rows[0].detail_url, DETAIL)
        self.assertEqual(pages, {1, 2})

    def test_parse_list_rejects_mismatched_detail_number(self):
        body = f'''<tr><th>序號</th><th>預計撥款日期</th><th>製表日期</th>
        <th>計畫流水號</th><th>備註</th><th>刪除</th><th>修改</th>
        <th>以舊清冊新增</th><th>詳細內容</th></tr>
        <tr><td>{NUMBER}</td><td></td><td></td><td></td><td></td><td></td>
        <td></td><td></td><td><a href="payloadlist_detail.do?serial_no=115p999999">詳細</a></td></tr>'''.encode()
        with self.assertRaises(ValueError):
            parse_list(body, "text/html", LIST_URL)

    def test_save_pdf_uses_official_print_link_and_private_file(self):
        row = AppropriationRow(NUMBER, "", "", "11421012", "", DETAIL)
        detail = f'''<html><a href="payloadlist_Q.do?isShowAll=true"
        onclick="javascript:window.open('payloadlist_P.do?serial_no={NUMBER}');">列印</a></html>'''.encode()
        pdf = b"%PDF-1.7\n" + b"a" * 200 + b"\n%%EOF"
        responses = [Response(DETAIL, detail, "text/html;charset=utf-8"),
                     Response(PRINT, pdf, "application/pdf")]
        with tempfile.TemporaryDirectory() as dirname, \
             patch("appropriation_list.open_page", side_effect=responses * 2) as request:
            path = save_pdf(object(), row, Path(dirname))
            self.assertEqual(path.name, f"印領清冊_{NUMBER}.pdf")
            self.assertEqual(path.read_bytes(), pdf)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                save_pdf(object(), row, Path(dirname))
            self.assertEqual(request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
