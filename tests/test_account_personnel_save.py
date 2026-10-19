"""Tests for personnel reimbursement saves."""

import json
import unittest
import urllib.parse
from unittest.mock import patch

from account_direct_save import SAVE_FIELDS
from account_personnel_save import prepare_personnel_save, validate_capture


class PersonnelSaveTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "abstract": "範例主持人費，請改為實際用途。",
            "budgetNo": "11421012", "amount": "25000",
            "payeeAmount": "25000", "payeeList": "yes",
        }
        self.cookies = {"ASPSESSIONIDSUQSQSST": "synthetic"}
        self.fields = {name: "" for name in SAVE_FIELDS}
        self.fields.update({
            "IsNew": "Y", "APPYYEAR": "115", "ADATE": "1151118",
            "APYDEP": "4030", "APYUSERNO": "A123456789",
            "ISAPPY": "True", "ISVOUC": "True", "ISTEMP": "False",
            "ISCASH": "False", "ISPURC": "False", "APPYSET": "15",
            "MOVETYPE": "6", "SETNAME": "人事費用報支",
            "ABSTRACT": self.record["abstract"], "NO_MATH": "N",
            "D_STR": "1#11421012#300#25000###6@",
            "A_STR": "#".join([
                "1", "CASHBOOK", "詳如清冊", "", "", "25000", "0",
                "1", "", "", "", "", "",
            ]) + "@",
        })

    def captured(self):
        return {
            "fields": [[name, value] for name, value in self.fields.items()],
            "main": {"category": "15", "plan": "11421012", "code": "300",
                     "amount": "25000", "move": "6"},
            "payee": {"name": "詳如清冊", "amount": "25000", "hasVendorId": True},
            "cookies": self.cookies,
        }

    def test_capture_requires_exact_personnel_category_and_payee(self):
        fields, cookies = validate_capture(self.captured(), self.record, self.cookies)
        self.assertEqual(fields["APPYSET"], "15")
        self.assertEqual(cookies, self.cookies)
        changed = self.captured()
        changed["fields"] = [[name, "14" if name == "APPYSET" else value]
                             for name, value in changed["fields"]]
        with self.assertRaisesRegex(ValueError, "APPYSET"):
            validate_capture(changed, self.record, self.cookies)
        changed = self.captured()
        changed["payee"]["name"] = "其他人"
        with self.assertRaisesRegex(ValueError, "詳如清冊"):
            validate_capture(changed, self.record, self.cookies)

    def test_capture_rejects_extra_payee_and_changed_amount(self):
        changed = self.captured()
        changed["fields"] = [[name, value + "2#X#其他@@@@" if name == "A_STR" else value]
                             for name, value in changed["fields"]]
        with self.assertRaisesRegex(ValueError, "筆數"):
            validate_capture(changed, self.record, self.cookies)
        changed = self.captured()
        changed["main"]["amount"] = "26000"
        with self.assertRaisesRegex(ValueError, "經費選擇"):
            validate_capture(changed, self.record, self.cookies)

    def test_prepare_only_captures_and_encodes_without_final_post(self):
        class FakeProcess:
            returncode = 0

            def communicate(self, input, timeout):
                payload = json.loads(input)
                self_test.assertEqual(payload["cookies"], self_test.cookies)
                self_test.assertIn("ncu-personnel-", payload["profile"])
                self_test.assertEqual(timeout, 120)
                return json.dumps(self_test.captured(), ensure_ascii=False).encode("utf-8"), b""

        self_test = self
        with patch("account_personnel_save.subprocess.Popen", return_value=FakeProcess()) as run:
            prepared = prepare_personnel_save(self.record, self.cookies, object(),
                                              "script CHK5 ATOLIS")
        self.assertEqual(prepared.budget_no, "11421012")
        self.assertEqual(prepared.amount, "25000")
        self.assertTrue(prepared.payee_verified)
        form = urllib.parse.parse_qs(prepared.body.decode("ascii"),
                                     keep_blank_values=True, encoding="cp950")
        self.assertEqual(form["APPYSET"], ["15"])
        self.assertEqual(form["ISCASH"], ["False"])
        self.assertEqual(form["A_STR"], [self.fields["A_STR"]])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(run.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
