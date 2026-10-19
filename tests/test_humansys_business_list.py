"""Tests for HumanSys business-trip listings."""

import unittest
import re

from humansys_business_list import (
    LIST_URL, parse_list, rows_for_start_date,
    selected_submit_form, validate_roc_date,
)


ROW = """
<table><tr>
<td data-th="申請單號">74643</td>
<td data-th="填單人">測試甲</td><td data-th="申請人">測試甲</td>
<td data-th="起日">1151027</td><td data-th="開始時間">08:00</td>
<td data-th="迄日">1151027</td><td data-th="結束時間">12:00</td>
<td data-th="本次申請時數">4</td><td data-th="給付方式"></td>
<td data-th="表單狀態">尚未送出</td><td data-th="異動人">測試甲</td>
<td data-th="功能"><form action="/HumanSys/personal/personalAttendApply/businessApply/74643"
method="post"><input name="_method" value="DELETE"><input name="_token" value="delete-token">
<button type="submit">刪除</button></form></td>
<td data-th="二代電子表單功能">
<form action="/HumanSys/personal/personalAttendApply/businessApply_dataToEform/74643"
method="post"><input type="hidden" name="_token" value="submit-token">
<button type="submit">送審</button></form></td>
</tr></table>
"""


class BusinessListTests(unittest.TestCase):
    def test_parse_and_select_exact_submission(self):
        rows = parse_list(ROW.encode(), "text/html; charset=utf-8", LIST_URL)
        self.assertEqual(len(rows), 1)
        row = rows_for_start_date(rows, "1151027")[0]
        self.assertEqual(row.application_id, "74643")
        self.assertEqual(row.values["本次申請時數"], "4")
        action, body = selected_submit_form(row, "1151027")
        self.assertEqual(action, LIST_URL.replace("/businessApply", "/businessApply_dataToEform/74643"))
        self.assertEqual(body, b"_token=submit-token")

    def test_rejects_date_and_status_mismatch(self):
        row = parse_list(ROW.encode(), "text/html", LIST_URL)[0]
        with self.assertRaisesRegex(ValueError, "起日"):
            selected_submit_form(row, "1151028")
        row.values["表單狀態"] = "簽核中"
        with self.assertRaisesRegex(ValueError, "尚未送出"):
            selected_submit_form(row, "1151027")

    def test_rejects_invalid_date(self):
        with self.assertRaises(ValueError):
            validate_roc_date("1150230")

    def test_server_html_without_data_th_uses_header_positions(self):
        headings = (
            "申請單號", "填單人", "申請人", "起日", "開始時間", "迄日",
            "結束時間", "本次申請時數", "給付方式", "表單狀態", "異動人",
            "功能", "二代電子表單功能",
        )
        header = "<tr>" + "".join(f"<th>{name}</th>" for name in headings) + "</tr>"
        server_html = re.sub(r' data-th="[^"]+"', "", ROW.replace("<table>", "<table>" + header))
        rows = parse_list(server_html.encode(), "text/html", LIST_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].application_id, "74643")
        self.assertEqual(rows[0].values["起日"], "1151027")
        self.assertEqual(selected_submit_form(rows[0], "1151027")[1], b"_token=submit-token")

if __name__ == "__main__":
    unittest.main()
