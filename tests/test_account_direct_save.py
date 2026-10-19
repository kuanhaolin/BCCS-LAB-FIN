"""Tests for guarded direct accounting saves."""

import re
import unittest
import urllib.parse
from unittest.mock import patch

from account_direct_save import (
    ACCOUNT_FRAME_PATHS, CATEGORY_PATH, LOOKUP_PATH, MAIN_COPY_FIELDS, SAVE_FIELDS, SAVE_PATH,
    TRAN_PATH, PreparedSave, SaveAttemptResult, _raw_page_signals, _trans_budget_codes,
    parse_form, prepare_save, send_save_once,
)


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


def input_tags(values):
    return "".join(f'<input name="{name}" value="{value}">'
                   for name, value in values.items())


class DirectSaveTests(unittest.TestCase):
    def test_raw_page_signals_do_not_expose_page_content(self):
        sample = ('<select><option value="11516004">私人計畫名稱</option></select>'
                  '<script>var T_A_BS=["11516004#300#私人用途"];</script>')
        message = _raw_page_signals(sample, "11516004")
        self.assertIn("原始 option 標籤 1", message)
        self.assertIn("原始計畫選項 有", message)
        self.assertIn("計畫與業務費資料片段 有", message)
        self.assertNotIn("私人", message)

    def record(self):
        return {
            "abstract": "測試用途", "budgetNo": "11516004", "budgetCode": "300",
            "budgetAmount": "1280", "product": "測試品名", "productAmount": "1280",
            "payeeAmount": "1280", "invoiceNo": "收據1150101",
            "invoiceDate": "1150101", "advancePaid": "yes",
        }

    def pages(self):
        main_values = {name: "" for name in MAIN_COPY_FIELDS}
        main_values.update({
            "IsNew": "Y", "APPYYEAR": "115", "ADATE": "1151016",
            "APYDEP": "4030", "APYUSERNO": "A123456789", "ISAPPY": "True",
            "ISVOUC": "True", "ISTEMP": "False", "ISCASH": "True",
            "ISPURC": "False", "APPYSET": "14", "MOVETYPE": "6",
            "SETNAME": "零用金先墊付報支（含國內旅費）", "NO_MATH": "N",
            "ISPERI": "N", "ISESS": "N",
        })
        main_values.update({
            "VOU_SERNO": "", "ALL_DEPT": "", "APPYBUY": "",
            "MOVETYPE_1": "6", "D_APYKIND_1": "", "SUBJECTNO_1": "",
            "D_AMOUNT_1": "0",
        })
        main = ('<form name="FORM1" method="post" action="STD_APPY_Q.asp">'
                + input_tags(main_values)
                + '<select name="BUGETNO_1"><option value="">選擇</option>'
                '<option value="11516004">計畫</option></select>'
                '<select name="BUGCODE_1"><option value="">選擇</option>'
                '<option value="300">業務費</option></select></form>'
                '<script>T_A_BS=BUGETS_STR.split("@");</script>')
        item = ('<form name="FORM1" method="post" action="STD_APPP_Q.asp">'
                + input_tags({"PRODUCT_1": "", "AMOUNT_1": "0", "QUANTITY_1": "1",
                              "SERUNIT_1": "式", "PLACE_1": "", "PTYPE_1": "",
                              "KEEPER_1": "", "KEEPERID_1": "", "KEEPERDNO_1": ""})
                + '</form>')
        payee = ('<form name="FORM1" method="post" action="STD_APPA_Q.asp">'
                 + input_tags({"VENDORID_1": "", "VENNAME_1": "", "AMOUNT_1": "0",
                               "PAYKIND_1": "1", "TAX_1": "0", "I_VENDORID_1": ""})
                 + '</form>')
        save_values = {name: "" for name in SAVE_FIELDS}
        save = ('<form name="FORM1" method="post" action="STD_SAVE_SE1_Q.asp">'
                + input_tags(save_values) + '</form>')
        lookup = ('<script>parent.MAIN.APPA.FORM1.VENDORID_1.value='
                  'CharToAsc("A123456789");'
                  'parent.MAIN.APPA.FORM1.VENNAME_1.value="測試人";'
                  'parent.MAIN.APPA.FORM1.BANKNO_1.value="";'
                  'parent.MAIN.APPA.FORM1.ACCOUNT_1.value="";'
                  'parent.MAIN.APPA.FORM1.ACCOUNTNAM_1.value="";'
                  '</script>')
        return {"APPY": main, "APPP": item, "APPA": payee, "PS": save,
                "LOOKUP": lookup,
                "TRAN": '<form name="FORM2"><input name="BUGETS" value=""></form>'}

    def test_trans_budget_codes_require_exact_budget_and_code_pair(self):
        content = ('<form name="FORM2"><input name="BUGETS" '
                   'value="115160040#300#other@11516004#100#人事費@'
                   '11516004#300#業務費@"></form>')
        appy = '<script>T_A_BS=BUGETS_STR.split("@");</script>'
        self.assertEqual(_trans_budget_codes(content, "11516004", appy), {"100", "300"})
        with self.assertRaisesRegex(ValueError, "唯一的 BUGETS"):
            _trans_budget_codes('<form name="FORM2"></form>', "11516004", appy)
        with self.assertRaisesRegex(ValueError, "分隔符號"):
            _trans_budget_codes(content, "11516004", "<script>T_A_BS=[];</script>")
        dollar_content = content.replace("@", "$")
        self.assertEqual(_trans_budget_codes(dollar_content, "11516004",
                                             appy.replace("@", "$")), {"100", "300"})

    def test_prepare_reads_forms_and_lookup_but_never_saves(self):
        pages = self.pages()
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == LOOKUP_PATH:
                    return Response(request.full_url, pages["LOOKUP"].encode("cp950"))
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            prepared = prepare_save(self.record(),
                                    {"ASPSESSIONIDSUQSQSST": "one",
                                     "ASPSESSIONIDSWRRSSTS": "two"}, object())
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "GET", "GET", "POST"])
        self.assertFalse(any(urllib.parse.urlsplit(request.full_url).path == SAVE_PATH
                             for request in requests))
        lookup = urllib.parse.parse_qs(requests[-1].data.decode())
        self.assertEqual(lookup["VENDORID"], ["A123456789"])
        payload = dict(urllib.parse.parse_qsl(prepared.body.decode(), encoding="cp950",
                                              keep_blank_values=True))
        self.assertEqual(list(payload), list(SAVE_FIELDS))
        self.assertEqual(payload["D_STR"], "1#11516004#300#1280###6@")
        self.assertEqual(payload["A_STR"],
                         "1#A123456789#測試人#1150101#收據1150101#1280#0#2#####@")
        self.assertEqual(payload["P_STR"], "1◎測試品名◎1◎式◎1280◎◎◎◎◎@")
        self.assertEqual(payload["ABSTRACT"], "測試用途")
        self.assertNotIn("測試人".encode("utf-8"), prepared.body)

    def test_prepare_rejects_missing_budget_before_payee_post(self):
        pages = self.pages()
        pages["APPY"] = pages["APPY"].replace('value="11516004"', 'value="99999999"')
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == TRAN_PATH:
                    return Response(request.full_url, pages["TRAN"].encode("cp950"))
                if path == CATEGORY_PATH:
                    return Response(request.full_url, b"<html>next</html>")
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(ValueError, "沒有所選計畫選項、沒有業務費 300 選項"):
                prepare_save(self.record(), {}, object())
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "POST", "GET", "GET", "GET", "GET"])
        self.assertEqual(urllib.parse.urlsplit(requests[1].full_url).path, TRAN_PATH)
        self.assertEqual(urllib.parse.urlsplit(requests[2].full_url).path, CATEGORY_PATH)

    def test_trans_budget_pair_validates_dynamic_options_without_final_save(self):
        pages = self.pages()
        pages["APPY"] = pages["APPY"].replace('<option value="11516004">計畫</option>', '')
        pages["APPY"] = pages["APPY"].replace('<option value="300">業務費</option>', '')
        pages["TRAN"] = ('<form name="FORM2"><input name="BUGETS" '
                         'value="11516004#100#人事費@11516004#300#業務費@"></form>')
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == TRAN_PATH:
                    return Response(request.full_url, pages["TRAN"].encode("cp950"))
                if path == CATEGORY_PATH:
                    return Response(request.full_url, b"<html>next</html>")
                if path == LOOKUP_PATH:
                    return Response(request.full_url, pages["LOOKUP"].encode("cp950"))
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            prepared = prepare_save(self.record(), {}, object())
        self.assertTrue(prepared.payee_verified)
        self.assertFalse(any(urllib.parse.urlsplit(request.full_url).path == SAVE_PATH
                             for request in requests))

    def test_trans_plan_without_business_code_stops_before_payee_lookup(self):
        pages = self.pages()
        pages["APPY"] = pages["APPY"].replace('<option value="11516004">計畫</option>', '')
        pages["APPY"] = pages["APPY"].replace('<option value="300">業務費</option>', '')
        pages["TRAN"] = ('<form name="FORM2"><input name="BUGETS" '
                         'value="11516004#100#人事費@"></form>')
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == TRAN_PATH:
                    return Response(request.full_url, pages["TRAN"].encode("cp950"))
                if path == CATEGORY_PATH:
                    return Response(request.full_url, b"<html>next</html>")
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(ValueError, "有所選計畫選項、沒有業務費 300 選項"):
                prepare_save(self.record(), {}, object())
        self.assertFalse(any(urllib.parse.urlsplit(request.full_url).path in {LOOKUP_PATH, SAVE_PATH}
                             for request in requests))

    def test_preloaded_budget_codes_validate_without_dynamic_option(self):
        pages = self.pages()
        pages["APPY"] = pages["APPY"].replace(
            '<option value="300">業務費</option>', '')
        pages["APPY"] += (
            '<script>var T_A_BS=["11516004#100#人事費",'
            '"11516004#300#業務費","11516004#333#管理費"];</script>')
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == LOOKUP_PATH:
                    return Response(request.full_url, pages["LOOKUP"].encode("cp950"))
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            prepared = prepare_save(self.record(), {}, object())
        self.assertTrue(prepared.payee_verified)
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "GET", "GET", "POST"])

    def test_uninitialized_frame_replays_next_step_without_saving(self):
        pages = self.pages()
        bare = pages["APPY"].replace('<option value="11516004">計畫</option>', '')
        bare = bare.replace('<option value="300">業務費</option>', '')
        initialized = False
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                nonlocal initialized
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == TRAN_PATH:
                    return Response(request.full_url, pages["TRAN"].encode("cp950"))
                if path == CATEGORY_PATH:
                    initialized = True
                    return Response(request.full_url, b"<html>next</html>")
                if path == LOOKUP_PATH:
                    return Response(request.full_url, pages["LOOKUP"].encode("cp950"))
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                content = pages[frame] if frame != "APPY" or initialized else bare
                return Response(request.full_url, content.encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            prepared = prepare_save(self.record(), {}, object())
        self.assertTrue(prepared.payee_verified)
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "POST", "GET", "GET", "GET", "GET", "POST"])
        self.assertEqual(urllib.parse.urlsplit(requests[1].full_url).path, TRAN_PATH)
        self.assertEqual(urllib.parse.urlsplit(requests[2].full_url).path, CATEGORY_PATH)
        self.assertFalse(any(urllib.parse.urlsplit(request.full_url).path == SAVE_PATH
                             for request in requests))

    def test_prepare_rejects_payee_identity_mismatch_without_save(self):
        pages = self.pages()
        pages["LOOKUP"] = pages["LOOKUP"].replace("A123456789", "B987654321")
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                path = urllib.parse.urlsplit(request.full_url).path
                if path == LOOKUP_PATH:
                    return Response(request.full_url, pages["LOOKUP"].encode("cp950"))
                frame = next(name for name, frame_path in ACCOUNT_FRAME_PATHS.items()
                             if frame_path == path)
                return Response(request.full_url, pages[frame].encode("cp950"))

        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(ValueError, "身分與申請人不一致"):
                prepare_save(self.record(), {}, object())
        self.assertEqual([request.get_method() for request in requests],
                         ["GET", "GET", "GET", "GET", "POST"])
        self.assertFalse(any(urllib.parse.urlsplit(request.full_url).path == SAVE_PATH
                             for request in requests))

    def test_final_save_is_one_post_to_exact_endpoint(self):
        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                return Response(request.full_url, "資料已存入".encode("cp950"))

        prepared = PreparedSave(None, object(), b"D_STR=x", "11516004", "1280", True)
        with patch("account_direct_save.urllib.request.build_opener", return_value=FakeOpener()):
            result = send_save_once(prepared)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(urllib.parse.urlsplit(requests[0].full_url).path, SAVE_PATH)
        self.assertEqual(result, SaveAttemptResult(200, SAVE_PATH, True))


if __name__ == "__main__":
    unittest.main()
