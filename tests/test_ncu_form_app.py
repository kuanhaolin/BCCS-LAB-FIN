"""Tests for the NCU form app's local web routes and forms."""

import io
import socket
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from pathlib import Path
import json
import tempfile
import urllib.parse

from humansys_business_list import BusinessRow, LIST_URL
from ncu_form_app import (
    ACCOUNT_SAVE_ATTEMPTS, ACCOUNT_SESSIONS, RESEARCH_SESSIONS, SESSIONS, APPROPRIATION_SESSIONS, APPROPRIATION_ADD_ATTEMPTS, Handler, LocalSession, START_CSRF, account_page,
    account_prefill_script, account_record_from_form, dashboard, list_page, load_account_defaults,
    load_personnel_defaults, personnel_record_from_form,
    research_page, research_list_page, research_human_page, appropriation_page,
    load_defaults, saved_account_cookies, business_report_list_page,
    appropriation_form_status, appropriation_add_payload, validate_appropriation_values, APPROPRIATION_FIELDS,
    _appropriation_submit_url, send_appropriation_once, APPROPRIATION_ADD_URL, APPROPRIATION_SUBMIT_URL,
    portal_cookie_from_header, portal_cookie_from_parts, record_from_form, save_json,
    saved_portal_cookie, automatic_portal_cookie, automatic_account_cookies,
    validate_account_record, PERSONNEL_DEFAULTS,
    personnel_page, personnel_prefill_script, validate_personnel_record,
)
from account_session_probe import (
    ACCOUNT_FRAME_FIELDS, ACCOUNT_FRAME_PATHS, ProbeResult,
    appy_frame_url,
    inspect_account_frames, inspect_payee_lookup,
)
from account_purchase_list import PurchaseRow
from humansys_business_report import BusinessReportRow
from research_budget import Project
from research_human_a import HumanPerson
from appropriation_list import AppropriationRow


class FakeServer:
    server_port = 8765


class WebTests(unittest.TestCase):
    def setUp(self):
        examples = Path(__file__).resolve().parents[1]
        defaults = patch.multiple(
            "ncu_form_app",
            INPUT_JSON=examples / "input_example.json",
            ACCOUNT_INPUT_JSON=examples / "input01_example.json",
            APPROPRIATION_INPUT_JSON=examples / "input02_example.json",
            PERSONNEL_INPUT_JSON=examples / "input03_example.json",
        )
        defaults.start()
        self.addCleanup(defaults.stop)

    def test_personnel_reimbursement_is_separate_and_never_saves_in_script(self):
        html = personnel_page().decode()
        self.assertIn('href="/account/personnel" aria-current="page"', html)
        self.assertIn('主持人費報帳 (主計室)', html)
        self.assertIn('>1. 報帳 Cookie</h2>', html)
        self.assertIn('貼上目前瀏覽器中的所有 ASPSESSIONID Cookie。', html)
        self.assertIn('action="/account/connect"', html)
        self.assertIn('name="returnTo" value="/account/personnel"', html)
        self.assertIn('name="accountCookie" type="password"', html)
        self.assertIn('手動更新', html)
        self.assertIn('action="/account/auto-connect"', html)
        self.assertIn('自動更新', html)
        self.assertIn('id="accountLoginAutoUpdate"', html)
        self.assertNotIn('accountExtensionStatus', html)
        self.assertIn('>2. 填寫報帳資料</h2>', html)
        self.assertIn('預設資料 (input03.json)', html)
        self.assertIn('name="payeeAmount" inputmode="numeric" required value=""', html)
        self.assertIn('name="payeeList" value="yes"', html)
        self.assertNotIn('name="payeeList" value="yes" checked', html)
        self.assertIn('>3. 列印憑證單</h2>', html)
        self.assertIn('name="category" value="15"', html)
        self.assertIn('action="/account/list"', html)
        self.assertNotIn('人事費用報支；經費用途固定為', html)
        self.assertNotIn('「存入」會讓官方頁面產生人事費表單', html)
        self.assertIn('action="/account/personnel/save"', html)
        self.assertIn('formaction="/account/personnel/prefill"', html)
        self.assertNotIn(PERSONNEL_DEFAULTS["abstract"], html)
        self.assertNotIn('value="11421012"', html)
        self.assertIn('300 業務費', html)
        self.assertNotIn('action="/account/save"', html)
        script = personnel_prefill_script(PERSONNEL_DEFAULTS)
        self.assertIn('x.name === "CHK5"', script)
        self.assertIn('"APPYSET").value !== "15"', script)
        self.assertIn('x.value.trim() === "詳如清冊"', script)
        self.assertIn('button.click(); // 使用官方 ATOLIS()', script)
        self.assertIn('return "filled";', script)
        self.assertNotIn('save.submit(', script)
        self.assertNotIn('STD_SAVE_SE1_Q.asp", {', script)

    def test_personnel_record_validation(self):
        validate_personnel_record(PERSONNEL_DEFAULTS)
        example = load_personnel_defaults()
        validate_personnel_record(example)
        self.assertEqual(example["budgetNo"], "TEST0001")
        self.assertEqual(personnel_record_from_form(PERSONNEL_DEFAULTS), PERSONNEL_DEFAULTS)
        loaded_html = personnel_page(values=example).decode()
        self.assertIn(example["abstract"], loaded_html)
        self.assertIn('value="TEST0001"', loaded_html)
        self.assertIn('name="amount" inputmode="numeric" required value="100"', loaded_html)
        self.assertIn('name="payeeAmount" inputmode="numeric" required value="100"', loaded_html)
        self.assertIn('name="payeeList" value="yes" checked', loaded_html)
        with self.assertRaisesRegex(ValueError, "金額"):
            validate_personnel_record({**PERSONNEL_DEFAULTS, "amount": "0"})
        with self.assertRaisesRegex(ValueError, "必須相同"):
            validate_personnel_record({**PERSONNEL_DEFAULTS, "payeeAmount": "24000"})
        with self.assertRaisesRegex(ValueError, "詳如清冊"):
            validate_personnel_record({**PERSONNEL_DEFAULTS, "payeeList": ""})
        with self.assertRaisesRegex(ValueError, "分隔字元"):
            validate_personnel_record({**PERSONNEL_DEFAULTS, "abstract": "wrong#value"})

    def test_personnel_defaults_button_loads_input03(self):
        handler = object.__new__(Handler)
        handler.path = "/account/personnel?load=input03"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output.decode()))
        handler.do_GET()
        self.assertEqual(pages[0][0], 200)
        self.assertIn(load_personnel_defaults()["abstract"], pages[0][1])
        self.assertIn('name="payeeList" value="yes" checked', pages[0][1])

    def test_removed_account_auto_script_is_not_served(self):
        handler = object.__new__(Handler)
        handler.path = "/account-auto.js"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append(
            (status, output.decode(), kwargs.get("content_type")))
        handler.do_GET()
        self.assertEqual(pages[0][0], 404)
        self.assertIn("只允許本機功能頁面", pages[0][1])

    def test_portal_session_status_has_same_position_on_three_pages(self):
        session = LocalSession(object(), "csrf", 1)
        pages = (dashboard(session, "Session 已保存。"), research_page(session),
                 appropriation_page(connected=True))
        for page_bytes in pages:
            with self.subTest(page=page_bytes[:80]):
                output = page_bytes.decode()
                self.assertEqual(output.count('<p class="ok">Session 已保存。</p>'), 1)
                first_card = output.split('<section class="card">', 1)[1].split('</section>', 1)[0]
                self.assertIn('action="/auto-connect"', first_card)
                self.assertLess(first_card.index('自動更新</button></form>'),
                                first_card.index('<p class="ok">Session 已保存。</p>'))

    def test_two_page_navigation_and_account_actions(self):
        business = dashboard(None).decode()
        self.assertIn('<a href="/" aria-current="page">公差申請 (人事系統)</a>', business)
        self.assertIn('>4. 列印申請單</h2>', business)
        self.assertIn('<a href="/account">公差報帳 (主計室)</a>', business)
        self.assertIn('action="/business-record/list"', business)
        account = account_page().decode()
        self.assertIn('<a href="/account" aria-current="page">公差報帳 (主計室)</a>', account)
        self.assertIn('action="/account/connect"', account)
        self.assertNotIn('action="/account/inspect"', account)
        self.assertNotIn('action="/account/payee-check"', account)
        self.assertIn('>1. 報帳 Cookie</h2>', account)
        self.assertIn('貼上目前瀏覽器中的所有 ASPSESSIONID Cookie。', account)
        self.assertIn('手動更新', account)
        self.assertIn('action="/account/auto-connect"', account)
        self.assertIn('自動更新', account)
        self.assertIn('id="accountLoginAutoUpdate"', account)
        self.assertNotIn('accountExtensionStatus', account)
        self.assertNotIn('留空時使用已保存的 session01.json', account)
        self.assertIn('預設資料 (input01.json)', account)
        self.assertIn('<button type="submit">存入</button>', account)
        self.assertNotIn('placeholder="ASPSESSIONID', account)
        self.assertIn('action="/account/save"', account)
        self.assertIn('action="/account/list"', account)
        self.assertIn('列出未審核案件', account)
        self.assertIn('3. 列印憑證單</h2>', account)
        self.assertNotIn('3. 列印 PDF', account)
        self.assertNotIn('清單會顯示零用金先墊付報支的未審核案件', account)
        self.assertNotIn('action="/account/prepare"', account)
        self.assertNotIn('action="/account/save-direct"', account)
        self.assertNotIn('確認存入一次', account)
        self.assertNotIn('action="/account/preview"', account)
        self.assertNotIn("預覽報帳資料（不存入）", account)
        self.assertIn('formaction="/account/prefill"', account)
        self.assertNotIn('formaction="/account/save-script"', account)
        self.assertNotIn('產生存入腳本', account)
        self.assertIn('產生填寫腳本（不存入）', account)
        self.assertIn('代墊（會自行帶入資料）', account)
        self.assertNotIn('代墊（報帳網站會自行帶入受款人資料）', account)
        self.assertIn('href="/account?load=input01"', account)
        self.assertIn('href="/research">計劃檢視 (研究發展系統)</a>', account)
        self.assertIn('href="/appropriation">主持人費撥帳 (撥帳系統)</a>', account)
        self.assertIn('href="/account/personnel">主持人費報帳 (主計室)</a>', account)
        self.assertLess(account.index('href="/appropriation"'),
                        account.index('href="/account/personnel"'))
        self.assertLess(account.index('href="/appropriation"'), account.index('href="/memo"'))
        self.assertIn('href="/memo">備忘錄</a>', account)

    def test_research_print_headings(self):
        session = LocalSession(object(), "csrf", 1)
        self.assertIn('>2. 列印經分表</h2>', research_page(session).decode())
        self.assertIn('>2. 列印經分表（0 筆）</h2>',
                      research_list_page(session, [], "").decode())

    def test_appropriation_page_has_local_add_and_list(self):
        handler = object.__new__(Handler)
        handler.path = "/appropriation"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output.decode()))
        handler.do_GET()
        self.assertEqual(pages[0][0], 200)
        self.assertIn('href="/appropriation" aria-current="page"', pages[0][1])
        self.assertIn('action="/appropriation/list"', pages[0][1])
        self.assertIn('>1. Session</h2>', pages[0][1])
        self.assertIn('主要包含 ① portal ② SESSION ③ XSRF-TOKEN。', pages[0][1])
        self.assertIn('name="returnTo" value="/appropriation"', pages[0][1])
        self.assertNotIn('appropriation-jsessionid', pages[0][1])
        self.assertIn('action="/appropriation/add"', pages[0][1])
        self.assertIn('>2. 填寫主持人費</h2>', pages[0][1])
        self.assertIn('>3. 列印印領清冊單</h2>', pages[0][1])
        self.assertLess(pages[0][1].index('>2. 填寫主持人費</h2>'),
                        pages[0][1].index('>3. 列印印領清冊單</h2>'))
        self.assertIn('href="/appropriation?load=input02"', pages[0][1])
        self.assertIn('data-field="isAccumulate"', pages[0][1])
        self.assertIn('data-field="is_techfund"', pages[0][1])
        self.assertNotIn('複製填寫腳本', pages[0][1])
        self.assertNotIn('action="/Appropriation/user/payloadlist_Sub.do"', pages[0][1])
        self.assertIn('<button type="submit">送出</button>', pages[0][1])
        self.assertNotIn('<button type="submit">加入</button>', pages[0][1])

    def test_appropriation_defaults_load_only_on_button(self):
        handler = object.__new__(Handler)
        handler.path = "/appropriation?load=input02"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output.decode()))
        defaults = {
            "pay_dept_no": "T430", "plan_sno": "11421012", "remarks": "",
            "isAccumulate": False, "is_techfund": True,
        }
        with patch("ncu_form_app.load_appropriation_defaults", return_value=defaults):
            handler.do_GET()
        self.assertEqual(pages[0][0], 200)
        self.assertIn('data-field="pay_dept_no" maxlength="10" value="T430"', pages[0][1])
        self.assertIn('data-field="plan_sno" maxlength="30" value="11421012"', pages[0][1])
        self.assertIn('data-field="is_techfund" checked', pages[0][1])
        self.assertIn('data-field="isAccumulate" >', pages[0][1])

    def test_appropriation_portal_connect_uses_sso(self):
        session = LocalSession(object(), "csrf", 999999999)
        with patch("ncu_form_app.bootstrap_appropriation_from_portal", return_value=session.opener) as connect, \
             patch("ncu_form_app.appropriation_form_status", return_value="form") as probe, \
             patch("ncu_form_app.save_json") as save:
            status, output = self._post_account_save(
                "unused", {"csrf": START_CSRF, "returnTo": "/appropriation",
                           "portalCookie": "portal=a; SESSION=b; XSRF-TOKEN=c"},
                path="/connect", cookie_name="hs_local_session")
        self.assertEqual(status, 200)
        self.assertIn("Session 已保存", output)
        self.assertNotIn("Portal 已自動切換並連線撥帳系統", output)
        self.assertNotIn("portal=a", output)
        connect.assert_called_once_with("portal=a; SESSION=b; XSRF-TOKEN=c")
        self.assertEqual(probe.call_count, 1)
        save.assert_called_once_with(unittest.mock.ANY,
                                     {"portalCookie": "portal=a; SESSION=b; XSRF-TOKEN=c"},
                                     private=True)
        APPROPRIATION_SESSIONS.clear()

    def test_appropriation_probe_does_not_treat_login_page_as_add_form(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def getcode(self):
                return 200

            def geturl(self):
                return "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_IPre.do"

            def read(self, _limit):
                return self.body.encode()

        login = '<form action="/Appropriation/login.do"><input name="login_id"><input name="password"></form>'
        fields = "".join(f'<input name="{name}">' for name in APPROPRIATION_FIELDS)
        add = '<form action="/Appropriation/user/payloadlist_Sub.do">' + fields + '</form>'
        for body, expected in ((login, "login"), (add, "form"), ("<p>Unknown</p>", "unknown")):
            with self.subTest(expected=expected), patch("ncu_form_app.open_page", return_value=Response(body)):
                self.assertEqual(appropriation_form_status(LocalSession(object(), "", 0)), expected)

    def test_appropriation_payload_matches_official_checkbox_rules(self):
        class Response:
            def __init__(self, flow="IuseO", index="0"):
                self.flow, self.index = flow, index

            def getcode(self):
                return 200

            def geturl(self):
                return "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_IPre.do"

            def read(self, _limit):
                fields = "".join(f'<input name="{name}">' for name in APPROPRIATION_FIELDS
                                 if name not in ("pay_dept_no", "address", "memo"))
                return ('''<form action="/Appropriation/user/payloadlist_Sub.do" method="post">
                <select name="pay_dept_no"><option value="T430">資管系</option></select>
                <textarea name="address"></textarea><textarea name="memo"></textarea>'''
                + fields + '''<input type="checkbox" name="isAccumulate" value="M1">
                <input type="checkbox" name="is_techfund" value="M1" checked>
                <input type="hidden" name="action" value="ADD">
                <input type="hidden" name="from" value="''' + self.flow + '''">
                <input type="hidden" name="index" value="''' + self.index + '''">
                <input type="hidden" name="calhealth" value="I">
                <input type="hidden" name="post_checked" value="false">
                <input type="hidden" name="isAccumulate_hidden" value="M2">
                <input type="hidden" name="is_techfund_hidden" value="M2">
                <input type="submit" name="Submit" value="加入"></form>''').encode()

        fields = {name: "" for name in APPROPRIATION_FIELDS}
        fields.update({"pay_dept_no": "T430", "plan_sno": "11421012", "title": "教授",
                       "name": "測試", "staff_personal_no": "A123456789",
                       "list_date": "11510", "ought_money": "25000",
                       "health2_money": "0", "actual_money": "25000",
                       "is_techfund": "on"})
        validate_appropriation_values(fields)
        for flow, index in (("I", "-1"), ("IuseO", "0")):
            with self.subTest(flow=flow), patch("ncu_form_app.open_page",
                                                return_value=Response(flow, index)):
                payload = urllib.parse.parse_qs(appropriation_add_payload(object(), fields).decode())
            self.assertEqual(payload["from"], [flow])
            self.assertEqual(payload["index"], [index])
            self.assertEqual(payload["isAccumulate_hidden"], ["M2"])
            self.assertNotIn("isAccumulate", payload)
            self.assertEqual(payload["is_techfund_hidden"], ["M1"])
            self.assertEqual(payload["is_techfund"], ["M1"])
            self.assertEqual(payload["Submit"], ["加入"])

    def test_appropriation_submit_url_uses_validated_values(self):
        values = {"pay_dept_no": "T430", "plan_sno": "11421012", "remarks": "",
                  "isAccumulate": "", "is_techfund": "on"}
        url = _appropriation_submit_url(values)
        self.assertEqual(urllib.parse.urlsplit(url).path,
                         "/Appropriation/user/payloadlist_IuseO.do")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query, {"a": [""], "pay_dept_no": ["T430"],
                                 "plan_sno": ["11421012"], "remarks": [""],
                                 "isAccumulate": ["M2"], "is_techfund": ["M1"],
                                 "calhealth": ["I"]})
        self.assertEqual(urllib.parse.parse_qs(
            urllib.parse.urlsplit(_appropriation_submit_url(
                {**values, "plan_sno": "11516004"})).query)["plan_sno"], ["11516004"])

    def test_appropriation_one_click_add_then_submit_then_list_check(self):
        class Response:
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def __init__(self, url, body):
                self.url, self.body = url, body

            def getcode(self):
                return 200

            def geturl(self):
                return self.url

            def read(self, _limit):
                return self.body

        values = {"pay_dept_no": "T430", "plan_sno": "11421012", "remarks": "",
                  "isAccumulate": "", "is_techfund": "on"}
        old = AppropriationRow("115p010224", "", "", "11421012", "", "")
        new = AppropriationRow("115p010225", "", "", "11421012", "", "")
        add = Response(APPROPRIATION_ADD_URL + "?from=I", b"<html>no submit button</html>")
        submitted = Response(APPROPRIATION_SUBMIT_URL, b"<html>submitted</html>")
        with patch("ncu_form_app.open_page", side_effect=[add, submitted]) as requests, \
             patch("ncu_form_app.fetch_appropriation_list",
                   return_value=([new, old], {1})):
            progress = ["idle"]
            number = send_appropriation_once(object(), b"add-payload", values,
                                            {old.number}, progress)
        self.assertEqual(number, new.number)
        self.assertEqual(progress, ["submitted"])
        self.assertEqual(requests.call_count, 2)
        self.assertEqual(requests.call_args_list[0].args[1], APPROPRIATION_ADD_URL)
        self.assertEqual(urllib.parse.urlsplit(requests.call_args_list[1].args[1]).path,
                         "/Appropriation/user/payloadlist_IuseO.do")
        self.assertEqual(requests.call_args_list[1].kwargs["referer"],
                         APPROPRIATION_ADD_URL + "?from=I")

    def test_appropriation_does_not_submit_after_unexpected_add_response(self):
        class Response:
            headers = {"Content-Type": "text/html"}

            def getcode(self):
                return 200

            def geturl(self):
                return "https://cis.ncu.edu.tw/Appropriation/login.do"

            def read(self, _limit):
                return b"<html>validation error</html>"

        values = {"pay_dept_no": "T430", "plan_sno": "11421012", "remarks": "",
                  "isAccumulate": "", "is_techfund": "on"}
        with patch("ncu_form_app.open_page", return_value=Response()) as requests, \
             patch("ncu_form_app.fetch_appropriation_list", return_value=([], {1})):
            with self.assertRaisesRegex(ValueError, "官方加入回應不符"):
                progress = ["idle"]
                send_appropriation_once(object(), b"add-payload", values, set(), progress)
        self.assertEqual(progress, ["add_response"])
        requests.assert_called_once()

    def test_appropriation_add_is_single_attempt(self):
        import time
        key = "synthetic-appropriation"
        session = LocalSession(object(), "csrf", time.monotonic() + 900)
        APPROPRIATION_SESSIONS[key] = session
        fields = {name: "" for name in APPROPRIATION_FIELDS}
        fields.update({"csrf": "csrf", "pay_dept_no": "T430", "plan_sno": "11421012",
                       "title": "教授", "name": "測試", "staff_personal_no": "A123456789",
                       "list_date": "11510", "ought_money": "25000",
                       "health2_money": "0", "actual_money": "25000"})
        try:
            with patch("ncu_form_app.appropriation_add_payload", return_value=b"test-payload") as prepare, \
                 patch("ncu_form_app.fetch_appropriation_list", return_value=([], {1})), \
                 patch("ncu_form_app.send_appropriation_once", return_value="115p010225") as send:
                status, page_html = self._post_account_save(
                    key, fields, "/appropriation/add", "app_local_session")
                self.assertEqual(status, 200)
                self.assertIn("已送出並在官方清單找到新紀錄：115p010225", page_html)
                status, page_html = self._post_account_save(
                    key, fields, "/appropriation/add", "app_local_session")
                self.assertEqual(status, 400)
                self.assertIn("15 分鐘內已嘗試加入相同資料", page_html)
                self.assertEqual(prepare.call_count, 2)
                self.assertEqual(send.call_args.args[:4],
                                 (session.opener, b"test-payload", fields, set()))
                self.assertEqual(send.call_args.args[4], ["idle"])
        finally:
            APPROPRIATION_SESSIONS.pop(key, None)
            for attempt in list(APPROPRIATION_ADD_ATTEMPTS):
                if attempt[0] == key:
                    del APPROPRIATION_ADD_ATTEMPTS[attempt]

    def test_appropriation_error_identifies_whether_final_submit_started(self):
        import time
        fields = {name: "" for name in APPROPRIATION_FIELDS}
        fields.update({"csrf": "csrf", "pay_dept_no": "T430", "plan_sno": "11421012",
                       "title": "教授", "name": "測試", "staff_personal_no": "A123456789",
                       "list_date": "11510", "ought_money": "25000",
                       "health2_money": "0", "actual_money": "25000"})
        for stage, expected in (("added", "「送出」尚未執行"),
                                ("submitting", "「送出」可能已執行")):
            key = "synthetic-appropriation-" + stage
            APPROPRIATION_SESSIONS[key] = LocalSession(object(), "csrf", time.monotonic() + 900)

            def fail_after_stage(_opener, _payload, _values, _previous, progress):
                progress[0] = stage
                raise ValueError("測試階段")

            try:
                with patch("ncu_form_app.fetch_appropriation_list", return_value=([], {1})), \
                     patch("ncu_form_app.appropriation_add_payload", return_value=b"test-payload"), \
                     patch("ncu_form_app.send_appropriation_once", side_effect=fail_after_stage):
                    status, output = self._post_account_save(
                        key, fields, "/appropriation/add", "app_local_session")
                self.assertEqual(status, 400)
                self.assertIn(expected, output)
            finally:
                APPROPRIATION_SESSIONS.pop(key, None)
                for attempt in list(APPROPRIATION_ADD_ATTEMPTS):
                    if attempt[0] == key:
                        del APPROPRIATION_ADD_ATTEMPTS[attempt]

    def test_appropriation_list_and_print_recheck_selected_row(self):
        import time
        key = "synthetic-appropriation-print"
        session = LocalSession(object(), "csrf", time.monotonic() + 900)
        APPROPRIATION_SESSIONS[key] = session
        row = AppropriationRow(
            "115p010224", "1151030", "1151017", "11421012", "<script>alert(1)</script>",
            "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_detail.do?serial_no=115p010224")
        try:
            with patch("ncu_form_app.fetch_appropriation_list", return_value=([row], {1, 2})) as fetch, \
                 patch("ncu_form_app.save_appropriation_pdf",
                       return_value=Path("/tmp/prints/主持人費_115p010224.pdf")) as save:
                status, listing = self._post_account_save(
                    key, {"csrf": "csrf", "page": "1"},
                    "/appropriation/list", "app_local_session")
                self.assertEqual(status, 200)
                self.assertIn("列印</button>", listing)
                self.assertIn("&lt;script&gt;", listing)
                self.assertNotIn("<script>alert(1)</script>", listing)
                status, printed = self._post_account_save(
                    key, {"csrf": "csrf", "page": "1", "number": row.number},
                    "/appropriation/print", "app_local_session")
                self.assertEqual(status, 200)
                self.assertIn("PDF 已儲存", printed)
                self.assertEqual(fetch.call_count, 2)
                save.assert_called_once_with(session.opener, row)
        finally:
            APPROPRIATION_SESSIONS.pop(key, None)

    def test_research_list_and_print_rechecks_selected_period(self):
        import time

        session_key = "synthetic-research"
        RESEARCH_SESSIONS[session_key] = LocalSession(
            object(), "research-csrf", time.monotonic() + 900)
        row = Project("23129", "11516004", "1", "雲端儲存", "甲", "資管系", "國網", "1150415", "1161130")
        try:
            with patch("ncu_form_app.fetch_projects", return_value=[row]) as fetch, \
                 patch("ncu_form_app.save_budget_pdf",
                       return_value=Path("/tmp/prints/經分表_11516004_第1期.pdf")) as save:
                status, listing = self._post_account_save(
                    session_key, {"csrf": "research-csrf", "isClose": ""},
                    path="/research/list", cookie_name="research_local_session")
                self.assertEqual(status, 200)
                self.assertIn("列印經分表", listing)
                status, result = self._post_account_save(
                    session_key, {"csrf": "research-csrf", "isClose": "",
                                  "budgetId": "23129", "number": "11516004", "period": "1"},
                    path="/research/print", cookie_name="research_local_session")
                self.assertEqual(status, 200)
                self.assertIn("經分表 PDF 已儲存", result)
                save.assert_called_once_with(unittest.mock.ANY, row)
                status, blocked = self._post_account_save(
                    session_key, {"csrf": "research-csrf", "isClose": "",
                                  "budgetId": "23129", "number": "11516004", "period": "2"},
                    path="/research/print", cookie_name="research_local_session")
                self.assertEqual(status, 400)
                self.assertIn("找不到唯一且相符", blocked)
                self.assertEqual(save.call_count, 1)
                self.assertEqual(fetch.call_count, 3)
        finally:
            RESEARCH_SESSIONS.pop(session_key, None)

    def test_research_甲式_form_and_print_recheck(self):
        import time

        session_key = "synthetic-research-human"
        session = LocalSession(object(), "research-csrf", time.monotonic() + 900)
        RESEARCH_SESSIONS[session_key] = session
        row = Project("18175", "6C403024", "1", "測試計畫", "甲", "資管系", "",
                      "1130718", "1530722")
        person = HumanPerson("BH_43323", "計畫主持人 · 測試人員")
        try:
            listing = research_list_page(session, [row], "").decode()
            self.assertIn('action="/research/human"', listing)
            form_page = research_human_page(session, row, [person], "").decode()
            self.assertIn('action="/research/human-print"', form_page)
            self.assertIn('value="BH_43323"', form_page)
            with patch("ncu_form_app.fetch_projects", return_value=[row]), \
                 patch("ncu_form_app.fetch_human_people", return_value=[person]) as people, \
                 patch("ncu_form_app.save_human_a_pdf",
                       return_value=Path("/tmp/prints/甲式.pdf")) as save:
                base = {"csrf": "research-csrf", "isClose": "", "budgetId": "18175",
                        "number": "6C403024", "period": "1"}
                status, shown = self._post_account_save(
                    session_key, base, "/research/human", "research_local_session")
                self.assertEqual(status, 200)
                self.assertIn("BH_43323", shown)
                people.assert_called_once_with(session.opener, "18175")
                status, printed = self._post_account_save(
                    session_key, {**base, "personKey": "BH_43323",
                                  "salaryStartDate": "11511", "salaryEndDate": "11511"},
                    "/research/human-print", "research_local_session")
                self.assertEqual(status, 200)
                self.assertIn("甲式 PDF 已儲存", printed)
                save.assert_called_once_with(session.opener, "18175", "BH_43323",
                                             "11511", "11511")
                status, blocked = self._post_account_save(
                    session_key, {**base, "period": "2", "personKey": "BH_43323",
                                  "salaryStartDate": "11511", "salaryEndDate": "11511"},
                    "/research/human-print", "research_local_session")
                self.assertEqual(status, 400)
                self.assertIn("找不到唯一且相符", blocked)
                self.assertEqual(save.call_count, 1)
        finally:
            RESEARCH_SESSIONS.pop(session_key, None)

    def test_research_page_waits_for_list_button(self):
        handler = object.__new__(Handler)
        handler.path = "/research"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output.decode(), kwargs))
        with patch("ncu_form_app.bootstrap_research_from_portal") as connect:
            handler.do_GET()
        self.assertEqual(pages[0][0], 200)
        self.assertIn("列出計劃", pages[0][1])
        self.assertNotIn("11516004", pages[0][1])
        self.assertNotIn("researchCookie", pages[0][1])
        connect.assert_not_called()

    def test_research_list_uses_saved_portal_only_on_click(self):
        row = Project("23129", "11516004", "1", "雲端儲存", "甲", "資管系", "國網", "1150415", "1161130")
        with patch("ncu_form_app.saved_portal_cookie", return_value="portal=a; SESSION=b; XSRF-TOKEN=c"), \
             patch("ncu_form_app.bootstrap_research_from_portal", return_value=(object(), [row])) as connect:
            status, output = self._post_account_save(
                "unused", {"csrf": START_CSRF, "isClose": ""},
                path="/research/list", cookie_name="research_local_session")
        self.assertEqual(status, 200)
        self.assertIn("11516004", output)
        connect.assert_called_once_with("portal=a; SESSION=b; XSRF-TOKEN=c")
        RESEARCH_SESSIONS.clear()

    def test_research_connect_accepts_same_portal_cookie_field(self):
        row = Project("23129", "11516004", "1", "雲端儲存", "甲", "資管系", "國網", "1150415", "1161130")
        with patch("ncu_form_app.save_json") as save, \
             patch("ncu_form_app.bootstrap_research_from_portal", return_value=(object(), [row])) as connect:
            status, output = self._post_account_save(
                "unused", {"csrf": START_CSRF,
                           "portalCookie": "portal=a; SESSION=b; XSRF-TOKEN=c"},
                path="/research/connect", cookie_name="research_local_session")
        self.assertEqual(status, 200)
        self.assertIn("Session 已保存", output)
        self.assertIn("列出計劃", output)
        self.assertNotIn("11516004", output)
        self.assertNotIn("portal=a", output)
        save.assert_called_once_with(unittest.mock.ANY,
                                     {"portalCookie": "portal=a; SESSION=b; XSRF-TOKEN=c"},
                                     private=True)
        connect.assert_called_once_with("portal=a; SESSION=b; XSRF-TOKEN=c")
        RESEARCH_SESSIONS.clear()

    def _post_account_save(self, session_key, fields, path="/account/save",
                           cookie_name="acc_local_session"):
        body = urllib.parse.urlencode(fields).encode()
        handler = object.__new__(Handler)
        handler.path = path
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Cookie"] = f"{cookie_name}={session_key}"
        handler.headers["Content-Type"] = "application/x-www-form-urlencoded"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output.decode()))
        handler.do_POST()
        return pages[0]

    def test_business_record_list_and_print_selected_row(self):
        import time

        session_key = "synthetic-human-report"
        session = LocalSession(None, "human-csrf", time.monotonic() + 900)
        SESSIONS[session_key] = session
        row = BusinessReportRow({
            "申請單號": "74643", "開始日期": "1151027", "結束日期": "1151027",
            "本次申請時數": "4", "表單狀態": "簽核完成",
        }, "https://cis.ncu.edu.tw/HumanSys/personal/personalAttend/personalBusinessRecordReport_print/74643")
        try:
            with patch("ncu_form_app.fetch_report", return_value=[row]) as fetch, \
                 patch("ncu_form_app.save_report_pdf",
                       return_value=Path("/tmp/prints/公差申請_74643.pdf")) as save:
                status, listing = self._post_account_save(
                    session_key, {"csrf": session.csrf}, "/business-record/list",
                    "hs_local_session")
                self.assertEqual(status, 200)
                self.assertIn('<th>操作</th>', listing)
                self.assertIn('name="number" value="74643"', listing)
                self.assertIn('>列印</button>', listing)
                status, result = self._post_account_save(
                    session_key, {"csrf": session.csrf, "number": "74643"},
                    "/business-record/print", "hs_local_session")
                self.assertEqual(status, 200)
                self.assertIn("公差申請_74643.pdf", result)
                self.assertEqual(fetch.call_count, 2)
                save.assert_called_once_with(session, row)
        finally:
            SESSIONS.pop(session_key, None)

    def test_business_record_row_without_verified_link_has_no_print_button(self):
        session = LocalSession(None, "human-csrf", 1)
        row = BusinessReportRow({
            "申請單號": "74643", "開始日期": "1151027", "結束日期": "1151027",
            "本次申請時數": "4", "表單狀態": "簽核完成",
        }, None)
        html = business_report_list_page(session, [row]).decode()
        self.assertIn("74643", html)
        self.assertNotIn('action="/business-record/print"', html)

    def test_account_list_shows_rows_with_print_action_then_prints_one(self):
        import time

        session_key = "synthetic-list-session"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        row = PurchaseRow("111540300202", "1151017", "測試 <script>alert(1)</script>",
                          "1,280", "未審核")
        try:
            with patch("ncu_form_app.fetch_purchase_list", return_value=[row]) as fetch, \
                 patch("ncu_form_app.save_purchase_pdf", return_value=Path("/tmp/prints/憑證單_111540300202.pdf")) as print_pdf, \
                 patch("ncu_form_app.send_save_once") as save:
                list_status, list_html = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115"}, "/account/list")
                status, page_html = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115", "number": "111540300202",
                                  "addition": ""},
                    "/account/print-pdf")
            self.assertEqual(list_status, 200)
            self.assertIn('name="number" value="111540300202"', list_html)
            self.assertIn('action="/account/print-pdf"', list_html)
            self.assertIn('<th>操作</th>', list_html)
            self.assertIn('>列印</button>', list_html)
            self.assertIn('&lt;script&gt;', list_html)
            self.assertNotIn('<script>alert(1)</script>', list_html)
            self.assertEqual(status, 200)
            self.assertIn("111540300202", page_html)
            self.assertIn("憑證單_111540300202.pdf", page_html)
            self.assertNotIn("<script>alert(1)</script>", page_html)
            self.assertEqual(fetch.call_count, 2)
            print_pdf.assert_called_once()
            save.assert_not_called()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_personnel_list_and_print_use_category_15_and_return_to_personnel(self):
        import time

        session_key = "synthetic-personnel-print"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        row = PurchaseRow("111540300203", "1151018", "主持人費", "25,000",
                          "未審核", "", "15", "NCU77.asp")
        try:
            with patch("ncu_form_app.fetch_purchase_list", return_value=[row]) as fetch, \
                 patch("ncu_form_app.save_purchase_pdf",
                       return_value=Path("/tmp/prints/憑證單_111540300203.pdf")) as print_pdf, \
                 patch("ncu_form_app.send_save_once") as save:
                list_status, list_html = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115", "category": "15"},
                    "/account/list")
                status, page_html = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115", "category": "15",
                                  "number": row.number, "addition": ""},
                    "/account/print-pdf")
            self.assertEqual(list_status, 200)
            self.assertIn('name="category" value="15"', list_html)
            self.assertIn('href="/account/personnel">返回報帳頁', list_html)
            self.assertEqual(status, 200)
            self.assertIn("憑證單_111540300203.pdf", page_html)
            self.assertIn('href="/account/personnel">返回報帳頁', page_html)
            self.assertEqual(fetch.call_count, 2)
            self.assertTrue(all(call.args[3] == "15" for call in fetch.call_args_list))
            print_pdf.assert_called_once()
            save.assert_not_called()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_account_print_pdf_rejects_bad_number_before_network(self):
        import time

        session_key = "synthetic-print-invalid"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        try:
            with patch("ncu_form_app.fetch_purchase_list") as fetch, \
                 patch("ncu_form_app.save_purchase_pdf") as print_pdf:
                status, message = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115", "number": "../bad",
                                  "addition": ""},
                    "/account/print-pdf")
            self.assertEqual(status, 400)
            self.assertIn("單號", message)
            fetch.assert_not_called()
            print_pdf.assert_not_called()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_account_print_pdf_rejects_changed_addition(self):
        import time

        session_key = "synthetic-print-changed-addition"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        row = PurchaseRow("111540300202", "1151017", "用途", "1,280", "未審核", "B")
        try:
            with patch("ncu_form_app.fetch_purchase_list", return_value=[row]), \
                 patch("ncu_form_app.save_purchase_pdf") as print_pdf:
                status, message = self._post_account_save(
                    session_key, {"csrf": START_CSRF, "year": "115", "number": row.number,
                                  "addition": "A"}, "/account/print-pdf")
            self.assertEqual(status, 400)
            self.assertIn("附號已變更", message)
            print_pdf.assert_not_called()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_account_save_sends_once_and_blocks_same_session_duplicate(self):
        import time
        from account_direct_save import SaveAttemptResult

        session_key = "synthetic-local-session"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        fields = {
            "csrf": START_CSRF, "abstract": "測試用途", "budgetNo": "11516004",
            "budgetAmount": "100", "product": "測試品名", "productAmount": "100",
            "payeeAmount": "100", "invoiceDate": "1151016", "advancePaid": "yes",
        }
        try:
            with patch("ncu_form_app.prepare_save", return_value=object()) as prepare, \
                 patch("ncu_form_app.send_save_once",
                       return_value=SaveAttemptResult(200,
                           "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp", True)) as send:
                status, page_html = self._post_account_save(session_key, fields)
                self.assertEqual(status, 200)
                self.assertIn("已發出一次報帳存入請求", page_html)
                self.assertNotIn("確認存入一次", page_html)
                status, page_html = self._post_account_save(session_key, fields)
                self.assertEqual(status, 400)
                self.assertIn("15 分鐘內嘗試存入相同資料", page_html)
                prepare.assert_called_once()
                send.assert_called_once()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)
            for key in list(ACCOUNT_SAVE_ATTEMPTS):
                if key[0] == session_key:
                    del ACCOUNT_SAVE_ATTEMPTS[key]

    def test_personnel_save_uses_separate_guard_and_single_final_post(self):
        import time
        from account_direct_save import SaveAttemptResult

        session_key = "synthetic-personnel-session"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        fields = {"csrf": START_CSRF, **PERSONNEL_DEFAULTS}
        try:
            with patch("ncu_form_app.prepare_personnel_save", return_value=object()) as prepare, \
                 patch("ncu_form_app.send_save_once",
                       return_value=SaveAttemptResult(200,
                           "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp", True)) as send:
                status, html = self._post_account_save(
                    session_key, fields, "/account/personnel/save")
                self.assertEqual(status, 200)
                self.assertIn("已發出一次人事費報帳存入請求", html)
                self.assertIn("資料已存入", html)
                status, html = self._post_account_save(
                    session_key, fields, "/account/personnel/save")
                self.assertEqual(status, 400)
                self.assertIn("15 分鐘內嘗試存入相同人事費資料", html)
                prepare.assert_called_once()
                send.assert_called_once()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)
            for key in list(ACCOUNT_SAVE_ATTEMPTS):
                if key[0] == session_key:
                    del ACCOUNT_SAVE_ATTEMPTS[key]

    def test_personnel_capture_failure_does_not_post_or_reserve(self):
        import time

        session_key = "synthetic-personnel-failed-capture"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        try:
            with patch("ncu_form_app.prepare_personnel_save",
                       side_effect=ValueError("官方欄位不同；沒有存入。")) as prepare, \
                 patch("ncu_form_app.send_save_once") as send:
                status, html = self._post_account_save(
                    session_key, {"csrf": START_CSRF, **PERSONNEL_DEFAULTS},
                    "/account/personnel/save")
                self.assertEqual(status, 400)
                self.assertIn("沒有存入", html)
                prepare.assert_called_once()
                send.assert_not_called()
                self.assertFalse(any(key[0] == session_key for key in ACCOUNT_SAVE_ATTEMPTS))
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_account_save_validation_failure_never_calls_final_save(self):
        import time

        session_key = "synthetic-prepare-session"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        fields = {
            "csrf": START_CSRF, "abstract": "測試用途", "budgetNo": "11516004",
            "budgetAmount": "100", "product": "測試品名", "productAmount": "100",
            "payeeAmount": "100", "invoiceDate": "1151016", "advancePaid": "yes",
        }
        try:
            with patch("ncu_form_app.prepare_save", side_effect=ValueError("官方資料不符；沒有存入。")) as prepare, \
                 patch("ncu_form_app.send_save_once") as send:
                status, page_html = self._post_account_save(session_key, fields)
                self.assertEqual(status, 400)
                self.assertIn("官方資料不符", page_html)
                prepare.assert_called_once()
                send.assert_not_called()
            self.assertFalse(any(key[0] == session_key for key in ACCOUNT_SAVE_ATTEMPTS))
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)

    def test_account_save_unclear_result_is_not_retried(self):
        import time

        session_key = "synthetic-unclear-session"
        ACCOUNT_SESSIONS[session_key] = ({"ASPSESSIONIDSUQSQSST": "fake"},
                                         time.monotonic() + 900)
        fields = {
            "csrf": START_CSRF, "abstract": "測試用途", "budgetNo": "11516004",
            "budgetAmount": "100", "product": "測試品名", "productAmount": "100",
            "payeeAmount": "100", "invoiceDate": "1151016", "advancePaid": "yes",
        }
        try:
            with patch("ncu_form_app.prepare_save", return_value=object()) as prepare, \
                 patch("ncu_form_app.send_save_once", side_effect=OSError("timeout")) as send:
                status, page_html = self._post_account_save(session_key, fields)
                self.assertEqual(status, 502)
                self.assertIn("結果不明", page_html)
                status, page_html = self._post_account_save(session_key, fields)
                self.assertEqual(status, 400)
                self.assertIn("15 分鐘內嘗試存入相同資料", page_html)
                prepare.assert_called_once()
                send.assert_called_once()
        finally:
            ACCOUNT_SESSIONS.pop(session_key, None)
            for key in list(ACCOUNT_SAVE_ATTEMPTS):
                if key[0] == session_key:
                    del ACCOUNT_SAVE_ATTEMPTS[key]

    def test_account_defaults_fill_page_without_submitting(self):
        record = load_account_defaults()
        self.assertEqual(record["budgetNo"], "TEST0001")
        self.assertEqual(record["budgetCode"], "300")
        self.assertEqual(record["advancePaid"], "yes")
        self.assertRegex(record["invoiceDate"], r"^\d{7}$")
        self.assertEqual(record["budgetAmount"], record["productAmount"])
        self.assertEqual(record["budgetAmount"], record["payeeAmount"])
        page_html = account_page(values=record).decode()
        self.assertIn('value="TEST0001"', page_html)
        self.assertIn(f'value="{record["invoiceNo"]}"', page_html)
        self.assertIn('name="advancePaid" value="yes" checked', page_html)
        self.assertNotIn("報帳填寫腳本", page_html)

    def test_account_record_checks_equal_amounts_and_roc_date(self):
        fields = {
            "abstract": "出差報支", "budgetNo": "11516004", "budgetAmount": "100",
            "product": "車資", "productAmount": "100", "payeeAmount": "100",
            "invoiceNo": "", "invoiceDate": "1151016", "advancePaid": "yes",
        }
        record = account_record_from_form(fields)
        self.assertEqual(record["budgetCode"], "300")
        validate_account_record(record)
        script = account_prefill_script(record)
        self.assertIn('window.top.document', script)
        self.assertIn('children.forEach(child => scan(child, forms, documents, seen))', script)
        self.assertIn('input.name === "CHK4"', script)
        self.assertNotIn('input.name === "CHK5"', script)
        self.assertIn('categoryField("APPYSET") !== "14"', script)
        self.assertIn('next.click()', script)
        self.assertIn('item: unique("品名"', script)
        self.assertIn('payee: unique("受款人"', script)
        self.assertIn('save: unique("儲存"', script)
        self.assertIn('select(main, "BUGETNO_1", data.budgetNo, "計畫", "change")', script)
        self.assertIn('select(main, "BUGCODE_1", "300", "業務費", "blur")', script)
        self.assertIn('await waitFrames("選擇計畫")', script)
        self.assertIn('"D_AMOUNT_1"', script)
        self.assertIn('"PRODUCT_1"', script)
        self.assertNotIn(".submit(", script)
        self.assertNotIn("SUM_ALERT(", script)
        self.assertNotIn("ASPSESSIONID", script)
        generated_page = account_page(True, values=record, prefill_script=script).decode()
        self.assertIn("報帳填寫腳本", generated_page)
        self.assertNotIn("切換到 MAIN 頁框", generated_page)
        with self.assertRaises(ValueError):
            validate_account_record({**record, "payeeAmount": "99"})
        with self.assertRaises(ValueError):
            validate_account_record({**record, "invoiceDate": "1150230"})
        with self.assertRaises(ValueError):
            account_prefill_script({**record, "advancePaid": "no"})

    def test_account_page_get_does_not_connect_to_humansys(self):
        for method, path, body in (
            ("GET", "/account", b""),
            ("GET", "/account?load=input01", b""),
        ):
            with self.subTest(path=path):
                request = (f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                           f"Content-Type: application/x-www-form-urlencoded\r\n"
                           f"Content-Length: {len(body)}\r\n\r\n").encode() + body
                server_sock, client_sock = socket.socketpair()
                try:
                    client_sock.sendall(request)
                    client_sock.shutdown(socket.SHUT_WR)
                    with patch("ncu_form_app.bootstrap") as bootstrap, \
                         patch("ncu_form_app.probe_account_session") as probe:
                        Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                        bootstrap.assert_not_called()
                        probe.assert_not_called()
                    server_sock.close()
                    result = bytearray()
                    while chunk := client_sock.recv(65536):
                        result.extend(chunk)
                    self.assertIn(b"HTTP/1.0 200 OK", result)
                    self.assertIn("報帳".encode(), result)
                    if path.endswith("load=input01"):
                        self.assertIn(b'value="TEST0001"', result)
                finally:
                    client_sock.close()
                    server_sock.close()

    def test_account_prefill_route_only_generates_script(self):
        fields = {
            "csrf": START_CSRF, "abstract": "出差報支", "budgetNo": "11516004",
            "budgetAmount": "100", "product": "車資", "productAmount": "100",
            "payeeAmount": "100", "advancePaid": "yes",
        }
        body = urllib.parse.urlencode(fields).encode()
        handler = object.__new__(Handler)
        handler.path = "/account/prefill"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/x-www-form-urlencoded"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        pages = []
        handler._send = lambda body, *args, **kwargs: pages.append(body)
        with patch("ncu_form_app.bootstrap") as bootstrap, \
             patch("ncu_form_app.probe_account_session") as probe:
            handler.do_POST()
            bootstrap.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(len(pages), 1)
        self.assertIn("報帳填寫腳本", pages[0].decode())

    def test_removed_save_script_route_has_no_effect(self):
        body = urllib.parse.urlencode({"csrf": START_CSRF}).encode()
        handler = object.__new__(Handler)
        handler.path = "/account/save-script"
        handler.server = FakeServer()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/x-www-form-urlencoded"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        pages = []
        handler._send = lambda output, status=200, **kwargs: pages.append((status, output))
        handler.do_POST()
        self.assertEqual(pages[0][0], 404)
        self.assertIn("不支援的操作", pages[0][1].decode())

    def test_account_frame_inspection_only_gets_expected_pages(self):
        class FakeResponse:
            status = 200

            def __init__(self, url, body):
                self.url, self.body = url, body

            def geturl(self):
                return self.url

            def read(self, size):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        requested = []

        class FakeOpener:
            def open(self, request, timeout):
                self_url = request.full_url
                requested.append((request.get_method(), self_url))
                frame = next(name for name, path in ACCOUNT_FRAME_PATHS.items()
                             if urllib.parse.urlsplit(self_url).path == path)
                fields = "".join(f'<input name="{name}">' for name in ACCOUNT_FRAME_FIELDS[frame])
                action = ("STD_SAVE_SE1_Q.asp" if frame == "PS" else "")
                body = f'<meta charset="big5"><form action="{action}">{fields}</form>'.encode()
                return FakeResponse(self_url, body)

        with patch("account_session_probe.urllib.request.build_opener", return_value=FakeOpener()):
            results = inspect_account_frames({"ASPSESSIONIDSUQSQSST": "one",
                                              "ASPSESSIONIDSWRRSSTS": "two"}, object())
        self.assertEqual(len(results), 4)
        self.assertTrue(all(item.status == 200 and item.correct_path and item.is_form
                            and not item.missing_fields and item.correct_save_action
                            for item in results))
        self.assertEqual([method for method, _ in requested], ["GET"] * 4)
        self.assertEqual(requested[0][1], appy_frame_url())
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(appy_frame_url(115)).query,
                                      keep_blank_values=True, encoding="cp950")["APPYSET"],
            ["14"],
        )
        self.assertIn("%B9s%A5%CE%AA%F7", appy_frame_url(115))

    def test_payee_lookup_uses_fresh_appy_identity_without_saving(self):
        class FakeResponse:
            status = 200

            def __init__(self, url, body):
                self.url, self.body = url, body

            def geturl(self):
                return self.url

            def read(self, size):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        requests = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                if request.get_method() == "GET":
                    body = (b'<form><input name="APPYSET" value="14">'
                            b'<input name="NO_MATH" value="N">'
                            b'<input name="APYUSERNO" value="A123456789"></form>')
                    return FakeResponse(request.full_url, body)
                self.assert_post(request)
                body = ('<script>parent.MAIN.APPA.FORM1.VENDORID_1.value='
                        'CharToAsc("A123456789");'
                        'parent.MAIN.APPA.FORM1.VENNAME_1.value="測試人";'
                        'parent.MAIN.APPA.FORM1.BANKNO_1.value="";'
                        '</script>').encode("cp950")
                return FakeResponse(request.full_url, body)

            def assert_post(self, request):
                self_url = urllib.parse.urlsplit(request.full_url)
                assert self_url.path == "/APSWIS_Q/Dept_Add_Q/CHK_VEN_Q.asp"
                payload = urllib.parse.parse_qs(request.data.decode())
                assert payload == {
                    "R_Sign": ["Y"], "VENDORID": ["A123456789"],
                    "RE_HAND": ["parent.MAIN.APPA.FORM1"], "I_J": ["_1"],
                    "PAYKIND": ["2"],
                }

        with patch("account_session_probe.urllib.request.build_opener", return_value=FakeOpener()):
            result = inspect_payee_lookup({"ASPSESSIONIDSUQSQSST": "one",
                                           "ASPSESSIONIDSWRRSSTS": "two"}, object())
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST"])
        self.assertTrue(result.identity_matches and result.payee_name_present)
        self.assertEqual(result.assignment_fields,
                         ("VENDORID_1", "VENNAME_1", "BANKNO_1"))

    def test_account_connect_saves_cookie_without_echoing_it(self):
        cookie = ("ASPSESSIONIDSWRRSSTS=one; ASPSESSIONIDSUQSQSST=two; "
                  "ASPSESSIONIDQWTSSQTS=three; ASPSESSIONIDSWSSTSSS=four; "
                  "ASPSESSIONIDSURRTSSS=five; ASPSESSIONIDQUSTSRST=six")
        body = urllib.parse.urlencode({
            "csrf": START_CSRF,
            "accountCookie": cookie,
        }).encode()
        request = (b"POST /account/connect HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                   b"Content-Type: application/x-www-form-urlencoded\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.sendall(request)
            client_sock.shutdown(socket.SHUT_WR)
            with patch("ncu_form_app.probe_account_session", side_effect=[
                ProbeResult(404, path, False, "anon"),
                ProbeResult(200, path, False, "logged"),
            ]) as probe, patch("ncu_form_app.save_json") as save:
                Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                self.assertEqual(probe.call_count, 2)
                save.assert_called_once()
                self.assertEqual(save.call_args.args[1], {"accountCookie": cookie})
                self.assertEqual(len(probe.call_args.args[0]), 6)
                self.assertTrue(save.call_args.kwargs["private"])
            server_sock.close()
            result = bytearray()
            while chunk := client_sock.recv(65536):
                result.extend(chunk)
            self.assertIn(b"HTTP/1.0 200 OK", result)
            self.assertIn(b"Set-Cookie: acc_local_session=", result)
            self.assertNotIn(cookie.encode(), result)
            self.assertNotIn("報帳 MAIN 頁面可讀".encode(), result)
        finally:
            ACCOUNT_SESSIONS.clear()
            client_sock.close()
            server_sock.close()

    def test_personnel_account_connect_returns_to_personnel_page(self):
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        fields = {
            "csrf": START_CSRF,
            "accountCookie": "ASPSESSIONIDSWRRSSTS=one; ASPSESSIONIDSUQSQSST=two",
            "returnTo": "/account/personnel",
        }
        try:
            with patch("ncu_form_app.probe_account_session", side_effect=[
                ProbeResult(404, path, False, "anon"),
                ProbeResult(200, path, False, "logged"),
            ]), patch("ncu_form_app.save_json"):
                status, html = self._post_account_save("none", fields, "/account/connect")
            self.assertEqual(status, 200)
            self.assertIn('href="/account/personnel" aria-current="page"', html)
            self.assertIn('>1. 報帳 Cookie</h2>', html)
            self.assertIn('Session 已保存。', html)
            self.assertNotIn("報帳 MAIN 頁面可讀", html)
            self.assertNotIn(fields["accountCookie"], html)
        finally:
            ACCOUNT_SESSIONS.clear()

    def test_account_session01_json_is_private_and_blank_input_reconnects(self):
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        cookie = "ASPSESSIONIDSUQSQSST=one; ASPSESSIONIDSWRRSSTS=two"
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            try:
                with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file), \
                     patch("ncu_form_app.probe_account_session", side_effect=[
                         ProbeResult(404, path, False, "anon"),
                         ProbeResult(200, path, False, "logged"),
                         ProbeResult(404, path, False, "anon"),
                         ProbeResult(200, path, False, "logged"),
                     ]) as probe:
                    status, page_html = self._post_account_save(
                        "none", {"csrf": START_CSRF, "accountCookie": cookie}, "/account/connect")
                    self.assertEqual(status, 200)
                    self.assertNotIn(cookie, page_html)
                    self.assertEqual(json.loads(session_file.read_text(encoding="utf-8")),
                                     {"accountCookie": cookie})
                    self.assertEqual(session_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(saved_account_cookies(), {
                        "ASPSESSIONIDSUQSQSST": "one", "ASPSESSIONIDSWRRSSTS": "two",
                    })
                    status, page_html = self._post_account_save(
                        "none", {"csrf": START_CSRF, "accountCookie": ""}, "/account/connect")
                    self.assertEqual(status, 200)
                    self.assertNotIn(cookie, page_html)
                    self.assertEqual(probe.call_count, 4)
                    self.assertEqual(probe.call_args.args[0], saved_account_cookies())
            finally:
                ACCOUNT_SESSIONS.clear()

    def test_account_connect_rejects_empty_session_without_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", Path(directory) / "session01.json"), \
                 patch("ncu_form_app.probe_account_session") as probe:
                status, message = self._post_account_save(
                    "none", {"csrf": START_CSRF, "accountCookie": ""}, "/account/connect")
            self.assertEqual(status, 400)
            self.assertIn("請貼上報帳 Cookie", message)
            probe.assert_not_called()

    def test_account_connect_does_not_replace_saved_cookie_when_probe_fails(self):
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        old_cookie = "ASPSESSIONIDSUQSQSST=old; ASPSESSIONIDSWRRSSTS=old"
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file):
                save_json(session_file, {"accountCookie": old_cookie}, private=True)
                with patch("ncu_form_app.probe_account_session", side_effect=[
                    ProbeResult(404, path, False, "anon"),
                    ProbeResult(404, path, False, "anon"),
                ]):
                    status, message = self._post_account_save(
                        "none", {"csrf": START_CSRF, "accountCookie":
                                 "ASPSESSIONIDSUQSQSST=new; ASPSESSIONIDSWRRSSTS=new"},
                        "/account/connect")
                self.assertEqual(status, 400)
                self.assertIn("沒有保存 Session", message)
                self.assertEqual(json.loads(session_file.read_text(encoding="utf-8")),
                                 {"accountCookie": old_cookie})

    def test_account_connect_accepts_logged_in_session_without_budget_options(self):
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        old_cookie = "ASPSESSIONIDSUQSQSST=old"
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            save_json(session_file, {"accountCookie": old_cookie}, private=True)
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file), \
                 patch("ncu_form_app.probe_account_session", side_effect=[
                     ProbeResult(404, path, False, "anon"),
                     ProbeResult(200, path, False, "logged"),
                 ]):
                status, message = self._post_account_save(
                    "none", {"csrf": START_CSRF,
                             "accountCookie": "ASPSESSIONIDSUQTTSSS=new"},
                    "/account/connect")
            self.assertEqual(status, 200)
            self.assertIn("Session 已保存。", message)
            self.assertEqual(json.loads(session_file.read_text()),
                             {"accountCookie": "ASPSESSIONIDSUQTTSSS=new"})

    def test_account_auto_connect_logs_in_and_saves_only_verified_session(self):
        old_cookie = "ASPSESSIONIDSUQSQSST=old"
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            save_json(session_file, {"accountCookie": old_cookie}, private=True)
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file), \
                 patch("ncu_form_app.automatic_account_cookies", return_value={
                     "ASPSESSIONIDSUQTTSSS": "new",
                 }) as isolated_login, \
                 patch("ncu_form_app.verify_account_cookies") as verify:
                status, body = self._post_account_save(
                    "none", {"csrf": START_CSRF}, "/account/auto-connect")
            isolated_login.assert_called_once_with()
            verify.assert_called_once_with({"ASPSESSIONIDSUQTTSSS": "new"})
            self.assertEqual(status, 200)
            self.assertIn("Session 已保存。", body)
            self.assertEqual(json.loads(session_file.read_text()),
                             {"accountCookie": "ASPSESSIONIDSUQTTSSS=new"})
            ACCOUNT_SESSIONS.clear()

    def test_account_auto_connect_keeps_saved_session_when_login_fails(self):
        old_cookie = "ASPSESSIONIDSUQSQSST=old"
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            save_json(session_file, {"accountCookie": old_cookie}, private=True)
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file), \
                 patch("ncu_form_app.automatic_account_cookies", side_effect=ValueError("登入失敗")):
                status, body = self._post_account_save(
                    "none", {"csrf": START_CSRF}, "/account/auto-connect")
            self.assertEqual(status, 400)
            self.assertIn("登入失敗", body)
            self.assertEqual(json.loads(session_file.read_text()),
                             {"accountCookie": old_cookie})

    def test_automatic_account_login_uses_temporary_session_only(self):
        expected = "ASPSESSIONIDSUQSQSST=new"

        def fake_login(command, **kwargs):
            self.assertNotIn(expected, command)
            self.assertEqual(command[-2], "--session")
            Path(command[-1]).write_text(json.dumps({"accountCookie": expected}))
            return Mock(returncode=0)

        with patch("ncu_form_app.subprocess.run", side_effect=fake_login):
            self.assertEqual(automatic_account_cookies(), {"ASPSESSIONIDSUQSQSST": "new"})
        with patch("ncu_form_app.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError, "原 Session 未變更"):
                automatic_account_cookies()
        with patch("ncu_form_app.subprocess.run", return_value=Mock(
                returncode=1, stdout="報帳 Session 未通過 MAIN 頁驗證".encode("utf-8"))):
            with self.assertRaisesRegex(ValueError, "MAIN 頁"):
                automatic_account_cookies()

    def test_account_auto_connect_personnel_uses_same_login(self):
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session01.json"
            with patch("ncu_form_app.ACCOUNT_SESSION_JSON", session_file), \
                 patch("ncu_form_app.automatic_account_cookies", return_value={
                     "ASPSESSIONIDSUQTTSSS": "new",
                 }) as isolated_login, \
                 patch("ncu_form_app.verify_account_cookies"):
                status, body = self._post_account_save(
                    "none", {"csrf": START_CSRF, "returnTo": "/account/personnel"},
                    "/account/auto-connect")
            isolated_login.assert_called_once_with()
            self.assertEqual(status, 200)
            self.assertIn('href="/account/personnel" aria-current="page"', body)
            self.assertTrue(session_file.exists())
            ACCOUNT_SESSIONS.clear()

    def test_dashboard_blank_until_explicit_load(self):
        defaults = load_defaults()
        page = dashboard(LocalSession(None, "local-csrf", 0)).decode()
        self.assertIn('name="startDate" required value=""', page)
        self.assertIn('name="proofFilename" required value=""', page)
        self.assertIn('name="startTime" type="text"', page)
        self.assertIn('name="endTime" type="text"', page)
        self.assertIn('placeholder="例如 17:00"', page)
        self.assertIn('href="/?load=input"', page)
        self.assertIn("預設資料 (input.json)", page)
        self.assertIn("startDate (民國115年1月1號，填1150101)", page)
        self.assertIn("proofFile[0].filename（本機證明檔案）", page)
        self.assertIn("destinationDistrict（407 臺中市 西屯區、744 臺南市 新市區）", page)
        self.assertIn("<h2>1. Session</h2>", page)
        self.assertIn("主要包含 ① portal ② SESSION ③ XSRF-TOKEN。", page)
        self.assertNotIn("請將 Cookie 合成一串貼入", page)
        self.assertIn('name="portalCookie" type="password"', page)
        for name in ('name="portal"', 'name="SESSION"', 'name="XSRF-TOKEN"'):
            self.assertNotIn(name, page)
        self.assertNotIn("./session.json", page)
        self.assertIn('<button type="submit">手動更新</button>', page)
        self.assertIn('<button type="submit">自動更新</button>', page)
        self.assertIn("我已核對資料</label>", page)
        self.assertIn("儲存 (不送審)", page)
        self.assertNotIn("目前儲存程式只支援", page)
        self.assertNotIn("本機 session.json 已有保存", page)
        self.assertNotIn("新開頁面一律空白", page)
        self.assertNotIn("起日（民國七碼，可留空）", page)
        loaded = dashboard(LocalSession(None, "local-csrf", 0), load_input=True).decode()
        self.assertIn(f'value="{defaults["startDate"]}"', loaded)
        self.assertIn(f'value="{defaults["startTime"]}"', loaded)

    def test_home_shows_all_operations_before_connection(self):
        page = dashboard(None).decode()
        self.assertIn("1. Session", page)
        self.assertIn("2. 填寫公差申請", page)
        self.assertIn("3. 查詢並送審", page)
        self.assertIn('name="date" aria-label="篩選起日" value=""', page)
        self.assertIn('action="/draft"', page)
        self.assertIn('action="/list"', page)

    def test_list_without_date_shows_all_and_only_unsent_can_be_submitted(self):
        rows = [BusinessRow({
            "申請單號": "74643", "填單人": "甲", "申請人": "甲", "起日": "1151027",
            "開始時間": "08:00", "迄日": "1151027", "結束時間": "12:00",
            "本次申請時數": "4", "給付方式": "", "表單狀態": "尚未送出", "異動人": "甲",
        }, LIST_URL.replace("/businessApply", "/businessApply_dataToEform/74643"), "token"),
        BusinessRow({
            "申請單號": "74403", "填單人": "甲", "申請人": "甲", "起日": "1151012",
            "開始時間": "13:00", "迄日": "1151012", "結束時間": "17:00",
            "本次申請時數": "4", "給付方式": "", "表單狀態": "簽核完成", "異動人": "甲",
        }, None, None)]
        page = list_page(LocalSession(None, "csrf", 0), "", rows, b"").decode()
        self.assertIn("目前清單中的全部申請", page)
        self.assertIn("74643", page)
        self.assertIn("74403", page)
        self.assertEqual(page.count('<button type="submit">送審</button>'), 1)
        self.assertIn('action="/submit"', page)
        self.assertNotIn('action="/review"', page)
        self.assertNotIn("SUBMIT 74643", page)
        self.assertIn('name="date" value="1151027"', page)

    def test_session_json_is_private_and_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with patch("ncu_form_app.SESSION_JSON", path):
                save_json(path, {"portalCookie": "SESSION=secret"}, private=True)
                self.assertEqual(saved_portal_cookie(), "SESSION=secret")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("SESSION=secret", dashboard(LocalSession(None, "csrf", 0)).decode())

    def test_cookie_header_and_legacy_file(self):
        self.assertEqual(portal_cookie_from_header(
            "Cookie: portal=abc; SESSION=def=; XSRF-TOKEN=xyz"),
            "portal=abc; SESSION=def=; XSRF-TOKEN=xyz")
        with self.assertRaises(ValueError):
            portal_cookie_from_header("SESSION=def; XSRF-TOKEN=xyz")
        with self.assertRaisesRegex(ValueError, "Portal Session 無法辨識") as malformed:
            portal_cookie_from_header("not-a-cookie")
        self.assertNotIn("NAME=value", str(malformed.exception))
        parts = {"portal": "portal=abc", "SESSION": "SESSION=def=", "XSRF-TOKEN": "xyz"}
        self.assertEqual(portal_cookie_from_parts(parts),
                         "portal=abc; SESSION=def=; XSRF-TOKEN=xyz")
        with self.assertRaises(ValueError):
            portal_cookie_from_parts({"portal": "abc", "SESSION": "def"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with patch("ncu_form_app.SESSION_JSON", path):
                save_json(path, {"portal": "abc", "SESSION": "def=", "XSRF-TOKEN": "xyz"},
                          private=True)
                self.assertEqual(saved_portal_cookie(),
                                 "portal=abc; SESSION=def=; XSRF-TOKEN=xyz")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_connect_button_saves_combined_cookie_without_echoing_it(self):
        body = urllib.parse.urlencode({
            "csrf": START_CSRF,
            "portalCookie": "portal=abc; SESSION=def=; XSRF-TOKEN=xyz",
        }).encode()
        request = (b"POST /connect HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                   b"Content-Type: application/x-www-form-urlencoded\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.sendall(request)
            client_sock.shutdown(socket.SHUT_WR)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "session.json"
                with patch("ncu_form_app.SESSION_JSON", path), \
                     patch("ncu_form_app.bootstrap", return_value=LocalSession(None, "csrf", 999999999)) as bootstrap:
                    Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                    bootstrap.assert_called_once_with(
                        "portal=abc; SESSION=def=; XSRF-TOKEN=xyz")
                self.assertEqual(json.loads(path.read_text()),
                                 {"portalCookie": "portal=abc; SESSION=def=; XSRF-TOKEN=xyz"})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            server_sock.close()
            client_sock.settimeout(2)
            result = bytearray()
            while chunk := client_sock.recv(65536):
                result.extend(chunk)
            self.assertIn(b"HTTP/1.0 200 OK", result)
            self.assertNotIn(b"portal=abc", result)
            self.assertNotIn(b"SESSION=def=", result)
        finally:
            client_sock.close()
            server_sock.close()

    def test_automatic_portal_login_uses_temporary_session_only(self):
        expected = "portal=abc; SESSION=def; XSRF-TOKEN=ghi"

        def fake_login(command, **kwargs):
            self.assertNotIn(expected, command)
            self.assertEqual(command[-2], "--session")
            Path(command[-1]).write_text(json.dumps({"portalCookie": expected}))
            return Mock(returncode=0)

        with patch("ncu_form_app.subprocess.run", side_effect=fake_login):
            self.assertEqual(automatic_portal_cookie(), expected)
        with patch("ncu_form_app.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError, "原 Session 未變更"):
                automatic_portal_cookie()

    def test_auto_connect_saves_only_after_target_connects(self):
        body = urllib.parse.urlencode({"csrf": START_CSRF}).encode()
        request = (b"POST /auto-connect HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                   b"Content-Type: application/x-www-form-urlencoded\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session.json"
            save_json(session_file, {"portalCookie": "old"}, private=True)
            for successful in (False, True):
                server_sock, client_sock = socket.socketpair()
                try:
                    client_sock.sendall(request)
                    client_sock.shutdown(socket.SHUT_WR)
                    with patch("ncu_form_app.SESSION_JSON", session_file), \
                         patch("ncu_form_app.automatic_portal_cookie",
                               return_value="portal=abc; SESSION=def; XSRF-TOKEN=ghi"), \
                         patch("ncu_form_app.bootstrap",
                               return_value=LocalSession(None, "csrf", 999999999)
                               if successful else None,
                               side_effect=None if successful else ValueError("連線失敗")):
                        Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                    server_sock.close()
                    client_sock.settimeout(2)
                    result = bytearray()
                    while chunk := client_sock.recv(65536):
                        result.extend(chunk)
                    if successful:
                        self.assertIn(b"HTTP/1.0 200 OK", result)
                        self.assertIn(b"hs_local_session=", result)
                        self.assertEqual(json.loads(session_file.read_text())["portalCookie"],
                                         "portal=abc; SESSION=def; XSRF-TOKEN=ghi")
                    else:
                        self.assertIn(b"HTTP/1.0 400 Bad Request", result)
                        self.assertEqual(json.loads(session_file.read_text())["portalCookie"], "old")
                    self.assertNotIn(b"XSRF-TOKEN=ghi", result)
                finally:
                    client_sock.close()
                    server_sock.close()

    def test_draft_sends_once_without_writing_form_json(self):
        source = load_defaults()
        fields = {key: str(source[key]) for key in (
            "startDate", "endDate", "startTime", "endTime", "destinationDistrict",
            "includeHoliday", "location", "reason",
        )}
        fields.update({"proofFilename": source["proofFile"][0]["filename"],
                       "operation": "save", "confirmDraft": "yes"})
        handler = object.__new__(Handler)
        session = LocalSession(None, "csrf", 0)
        with patch("ncu_form_app.prepare_record", return_value={"ok": True}) as prepare, \
             patch("ncu_form_app.save_json") as save, \
             patch.object(handler, "_send_draft_once") as send:
            handler._handle_draft(session, fields, None)
            prepare.assert_called_once_with(source, Path(__file__).resolve().parents[1])
            save.assert_not_called()
            send.assert_called_once_with(session, {"ok": True})

    def test_get_home_http_response_has_no_store(self):
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n\r\n")
            client_sock.shutdown(socket.SHUT_WR)
            Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
            server_sock.close()
            client_sock.settimeout(2)
            result = bytearray()
            while chunk := client_sock.recv(65536):
                result.extend(chunk)
            self.assertIn(b"HTTP/1.0 200 OK", result)
            self.assertIn(b"Cache-Control: no-store", result)
            self.assertIn("主要包含 ① portal ② SESSION ③ XSRF-TOKEN。".encode(), result)
            self.assertIn("填寫公差申請".encode(), result)
            self.assertIn("查詢並送審".encode(), result)
        finally:
            client_sock.close()
            server_sock.close()

    def test_post_list_blank_date_uses_all_rows(self):
        body = urllib.parse.urlencode({"csrf": START_CSRF, "date": ""}).encode()
        row = BusinessRow({
            "申請單號": "74643", "填單人": "甲", "申請人": "甲", "起日": "1151027",
            "開始時間": "08:00", "迄日": "1151027", "結束時間": "12:00",
            "本次申請時數": "4", "給付方式": "", "表單狀態": "尚未送出", "異動人": "甲",
        }, LIST_URL.replace("/businessApply", "/businessApply_dataToEform/74643"), "token")
        for origin in (b"", b"Origin: null\r\n", b"Origin: https://example.invalid\r\n"):
            with self.subTest(origin=origin):
                request = (b"POST /list HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                           + origin + b"Content-Type: application/x-www-form-urlencoded\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                server_sock, client_sock = socket.socketpair()
                try:
                    client_sock.sendall(request)
                    client_sock.shutdown(socket.SHUT_WR)
                    with patch("ncu_form_app.saved_portal_cookie", return_value="SESSION=test"), \
                         patch("ncu_form_app.bootstrap", return_value=LocalSession(None, "csrf", 999999999)), \
                         patch("ncu_form_app.fetch_list", return_value=([row], b"<table></table>")):
                        Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                    server_sock.close()
                    client_sock.settimeout(2)
                    result = bytearray()
                    while chunk := client_sock.recv(65536):
                        result.extend(chunk)
                    self.assertIn(b"HTTP/1.0 200 OK", result)
                    self.assertIn("目前清單中的全部申請".encode(), result)
                    self.assertIn(b"74643", result)
                finally:
                    client_sock.close()
                    server_sock.close()

    def test_foreign_origin_without_valid_csrf_still_rejected(self):
        body = urllib.parse.urlencode({"csrf": "wrong", "date": ""}).encode()
        request = (b"POST /list HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                   b"Origin: https://example.invalid\r\n"
                   b"Content-Type: application/x-www-form-urlencoded\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.sendall(request)
            client_sock.shutdown(socket.SHUT_WR)
            with patch("ncu_form_app.bootstrap") as bootstrap:
                Handler(server_sock, ("127.0.0.1", 12345), FakeServer())
                bootstrap.assert_not_called()
            server_sock.close()
            result = client_sock.recv(65536)
            self.assertIn(b"HTTP/1.0 400 Bad Request", result)
        finally:
            client_sock.close()
            server_sock.close()

    def test_parses_multipart_utf8_upload(self):
        boundary = "web-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"startDate\"\r\n\r\n1151027\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"proofFile\"; filename=\"proof.png\"\r\n"
            "Content-Type: image/png\r\n\r\nPNGDATA\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        handler = object.__new__(Handler)
        headers = Message()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(body))
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        fields, upload = handler._read_form()
        self.assertEqual(fields["startDate"], "1151027")
        self.assertEqual(upload, ("proof.png", b"PNGDATA"))

    def test_record_from_form_keeps_explicit_fields(self):
        record = record_from_form({"startDate": "1151027", "startTime": "08:00"},
                                  ["proof.png"])
        self.assertEqual(record["startDate"], "1151027")
        self.assertEqual(record["startTime"], "08:00")
        self.assertEqual(record["proofFile"], [{"filename": "proof.png"}])

    def test_submit_posts_only_once_without_second_confirmation(self):
        values = {
            "申請單號": "74643", "填單人": "甲", "申請人": "甲", "起日": "1151027",
            "開始時間": "08:00", "迄日": "1151027", "結束時間": "12:00",
            "本次申請時數": "4", "給付方式": "", "表單狀態": "尚未送出", "異動人": "甲",
        }
        action = LIST_URL.replace("/businessApply", "/businessApply_dataToEform/74643")
        before = BusinessRow(values, action, "server-token")
        after = BusinessRow({**values, "表單狀態": "簽核中"}, None, None)
        handler = object.__new__(Handler)
        sent_pages = []
        handler._send = lambda body, *args, **kwargs: sent_pages.append(body)
        session = LocalSession(None, "csrf", 0)

        class Response:
            def read(self, limit):
                return b"ok"

        with patch("ncu_form_app.fetch_list", side_effect=[([before], b""), ([after], b"")]), \
             patch("ncu_form_app.open_page", return_value=Response()) as post:
            handler._handle_submit(session, {
                "date": "1151027", "applicationId": "74643",
            })
            post.assert_called_once()
            self.assertEqual(post.call_args.args[1], action)
            self.assertEqual(post.call_args.kwargs["data"], b"_token=server-token")
            self.assertIn("簽核中", sent_pages[0].decode())

    def test_submit_stale_status_never_posts(self):
        handler = object.__new__(Handler)
        values = {
            "申請單號": "74643", "填單人": "甲", "申請人": "甲", "起日": "1151027",
            "開始時間": "08:00", "迄日": "1151027", "結束時間": "12:00",
            "本次申請時數": "4", "給付方式": "", "表單狀態": "簽核中", "異動人": "甲",
        }
        row = BusinessRow(values, None, None)
        with patch("ncu_form_app.open_page") as post:
            with patch("ncu_form_app.fetch_list", return_value=([row], b"")):
                with self.assertRaisesRegex(ValueError, "尚未送出"):
                    handler._handle_submit(LocalSession(None, "csrf", 0), {
                        "date": "1151027", "applicationId": "74643",
                    })
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
