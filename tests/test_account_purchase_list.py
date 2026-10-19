"""Tests for accounting case listing."""

import unittest
import urllib.parse
from unittest.mock import patch

from account_purchase_list import (
    LIST_PATH, PRINT_PATH, TITLE_PATH, PurchaseRow, fetch_purchase_list,
    parse_purchase_rows, purchase_print_url,
)


def listing_page(category="11", choice="2", rows=""):
    return f'''<form name="FORM1" action="DA_SerSTART_Q.asp" method="post">
<input name="APPYYEAR" value="115"><input name="CHKVAL" value="{choice}">
<input name="APYNO" value=""><input name="APYADD" value="">
<input name="USERNO" value="private-identifier">
<select name="APPYSET"><option value="11" {"selected" if category == "11" else ""}>其他</option>
<option value="14" {"selected" if category == "14" else ""}>零用金</option>
<option value="15" {"selected" if category == "15" else ""}>人事費用</option></select>
<select name="CHKVAL_S"><option value="2" {"selected" if choice == "2" else ""}>未結案</option>
<option value="4" {"selected" if choice == "4" else ""}>未審核</option></select>
{rows}</form>'''


TITLE_PAGE = '''<script>window.location=("DA_SerStart_Q.asp?RNK=N101747134&APPYYEAR=115");</script>'''


SAMPLE_ROW = '''<tr><td><input type="radio" name="R" onclick="MR_1();">
<script>function MR_1() { FORM1.APYNO.value="111540300202";
FORM1.APYADD.value=""; FORM1.APPYSET.value="14";
FORM1.PR_Href.value="NCU33.asp"; }</script>
<font>111540300202</font></td><td>類別</td><td>1151017</td>
<td>差旅費測試</td><td>1,280&nbsp;</td><td>否</td><td>未審核</td></tr>'''
PERSONNEL_ROW = SAMPLE_ROW.replace('APPYSET.value="14"', 'APPYSET.value="15"').replace(
    'NCU33.asp', 'NCU77.asp').replace('差旅費測試', '主持人費')


class Response:
    status = 200

    def __init__(self, url, body):
        self.url = url
        self.body = body

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PurchaseListTests(unittest.TestCase):
    def test_extracts_only_verified_row(self):
        self.assertEqual(parse_purchase_rows(listing_page("14", "4", SAMPLE_ROW)), [
            PurchaseRow("111540300202", "1151017", "差旅費測試", "1,280", "未審核")
        ])
        with self.assertRaisesRegex(ValueError, "類別"):
            parse_purchase_rows(SAMPLE_ROW.replace('APPYSET.value="14"', 'APPYSET.value="15"'))

    def test_print_url_uses_selected_row_values_only(self):
        row = PurchaseRow("111540300202", "1151017", "差旅費", "1,280", "未審核", "A")
        url = purchase_print_url(row, 115)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.hostname, "account.ncu.edu.tw")
        self.assertEqual(parts.path, PRINT_PATH)
        self.assertEqual(urllib.parse.parse_qs(parts.query), {
            "APPYYEAR": ["115"], "APYNO": ["111540300202"],
            "APYADD": ["A"], "APPYSET": ["14"],
        })
        with self.assertRaises(ValueError):
            purchase_print_url(PurchaseRow("1", "1151017", "", "1", "", "../bad"), 115)

    def test_personnel_row_keeps_official_category_and_print_target(self):
        rows = parse_purchase_rows(listing_page("15", "4", PERSONNEL_ROW), "15")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category, "15")
        self.assertEqual(rows[0].print_href, "NCU77.asp")
        url = urllib.parse.urlsplit(purchase_print_url(rows[0], 115))
        self.assertEqual(url.path, "/APSWIS_Q/TMP_PAG_Q/NCU77.asp")
        self.assertEqual(urllib.parse.parse_qs(url.query)["APPYSET"], ["15"])
        with self.assertRaisesRegex(ValueError, "格式"):
            parse_purchase_rows(listing_page("15", "4",
                PERSONNEL_ROW.replace("NCU77.asp", "../bad.asp")), "15")

    def test_read_only_filter_uses_official_form_and_never_saves(self):
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                if urllib.parse.urlsplit(request.full_url).path == TITLE_PATH:
                    content = TITLE_PAGE
                elif request.get_method() == "GET":
                    content = listing_page()
                else:
                    fields = urllib.parse.parse_qs(request.data.decode("ascii"),
                                                   keep_blank_values=True)
                    category = fields["APPYSET"][0]
                    choice = fields["CHKVAL_S"][0]
                    content = listing_page(category, choice,
                                           SAMPLE_ROW if category == "14" and choice == "4" else "")
                return Response(request.full_url, content.encode("cp950"))

        with patch("account_purchase_list.urllib.request.build_opener", return_value=FakeOpener()):
            rows = fetch_purchase_list({}, object(), 115)
        self.assertEqual(len(rows), 1)
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "POST", "POST"])
        self.assertEqual(urllib.parse.urlsplit(requests[0].full_url).path, TITLE_PATH)
        self.assertTrue(all(urllib.parse.urlsplit(request.full_url).path.lower()
                            == LIST_PATH.lower() for request in requests[1:]))
        self.assertEqual(urllib.parse.parse_qs(
            urllib.parse.urlsplit(requests[1].full_url).query)["RNK"], ["N101747134"])
        self.assertEqual(urllib.parse.parse_qs(requests[-1].data.decode("ascii"))["CHKVAL_S"], ["4"])

    def test_personnel_filter_uses_category_15(self):
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                if urllib.parse.urlsplit(request.full_url).path == TITLE_PATH:
                    content = TITLE_PAGE
                elif request.get_method() == "GET":
                    content = listing_page()
                else:
                    fields = urllib.parse.parse_qs(request.data.decode("ascii"),
                                                   keep_blank_values=True)
                    category = fields["APPYSET"][0]
                    choice = fields["CHKVAL_S"][0]
                    content = listing_page(category, choice,
                                           PERSONNEL_ROW if category == "15" and choice == "4" else "")
                return Response(request.full_url, content.encode("cp950"))

        with patch("account_purchase_list.urllib.request.build_opener", return_value=FakeOpener()):
            rows = fetch_purchase_list({}, object(), 115, "15")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category, "15")
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "POST", "POST"])

    def test_refuses_changed_form_action(self):
        class FakeOpener:
            def open(self, request, timeout):
                content = (TITLE_PAGE if urllib.parse.urlsplit(request.full_url).path == TITLE_PATH
                           else listing_page().replace('action="DA_SerSTART_Q.asp"',
                                                       'action="STD_SAVE_SE1_Q.asp"'))
                return Response(request.full_url, content.encode("cp950"))

        with patch("account_purchase_list.urllib.request.build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(ValueError, "目標已變更"):
                fetch_purchase_list({}, object(), 115)

    def test_missing_or_wrong_year_title_link_never_queries_list(self):
        for title in ('<script>window.location="DA_SerStart_Q.asp?APPYYEAR=115"</script>',
                      TITLE_PAGE.replace("APPYYEAR=115", "APPYYEAR=114"),
                      TITLE_PAGE.replace("N101747134", "bad%0d%0a")):
            requests = []

            class FakeOpener:
                def open(self, request, timeout):
                    requests.append(request)
                    return Response(request.full_url, title.encode("cp950"))

            with patch("account_purchase_list.urllib.request.build_opener",
                       return_value=FakeOpener()):
                with self.assertRaisesRegex(ValueError, "連結無法唯一核對"):
                    fetch_purchase_list({}, object(), 115)
            self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
