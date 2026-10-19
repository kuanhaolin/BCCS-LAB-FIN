"""Tests for accounting session and frame inspection."""

import ssl
import unittest
import urllib.parse
import urllib.request

from account_session_probe import (
    MAIN_FRAME_TARGET, TARGET, BudgetOptions, OffsiteRedirect, ProbeResult, SameHostRedirect,
    assessment, cookie_jar, cookie_value, cookies_from_header,
)


class AccountSessionProbeTests(unittest.TestCase):
    def test_budget_options_ignore_placeholder_and_other_selectors(self):
        parser = BudgetOptions()
        parser.feed('''<select name="OTHER"><option value="x">other</option></select>
            <select name="BUGETNO_1"><option value="">choose</option>
            <option value="11516004">project</option><option value="11421012">project</option>
            </select>''')
        self.assertEqual(parser.count, 2)

    def test_cookie_value_accepts_name_or_value_but_not_header_injection(self):
        name = "ASPSESSIONIDSUQSQSST"
        self.assertEqual(cookie_value("  abc=123  ", name), "abc=123")
        self.assertEqual(cookie_value(f"{name}=abc=123", name), "abc=123")
        for bad in ("", "abc; OTHER=secret", "abc\r\nX: value"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cookie_value(bad, name)

    def test_combined_cookie_header_accepts_any_number_of_session_names(self):
        header = ("Cookie: ASPSESSIONIDSUQSQSST=one=1; "
                  "ASPSESSIONIDSWRRSSTS=two=2")
        self.assertEqual(cookies_from_header(header), {
            "ASPSESSIONIDSUQSQSST": "one=1",
            "ASPSESSIONIDSWRRSSTS": "two=2",
        })
        self.assertEqual(cookies_from_header(
            header + "; ASPSESSIONIDQWTSSQTS=three=3"
        ), {
            "ASPSESSIONIDSUQSQSST": "one=1",
            "ASPSESSIONIDSWRRSSTS": "two=2",
            "ASPSESSIONIDQWTSSQTS": "three=3",
        })
        self.assertEqual(cookies_from_header(
            header + "; ASPSESSIONIDQWTSSQTS=three=3; ASPSESSIONIDSWSSTSSS=four=4"
        ), {
            "ASPSESSIONIDSUQSQSST": "one=1",
            "ASPSESSIONIDSWRRSSTS": "two=2",
            "ASPSESSIONIDQWTSSQTS": "three=3",
            "ASPSESSIONIDSWSSTSSS": "four=4",
        })
        self.assertEqual(cookies_from_header("ASPSESSIONIDSURRTSSS=one"),
                         {"ASPSESSIONIDSURRTSSS": "one"})
        six = (header + "; ASPSESSIONIDQWTSSQTS=three; ASPSESSIONIDSWSSTSSS=four"
               "; ASPSESSIONIDSURRTSSS=five; ASPSESSIONIDQUSTSRST=six")
        self.assertEqual(len(cookies_from_header(six)), 6)
        for bad in (
            "",
            "ASPSESSIONIDSUQSQSST=one; ASPSESSIONIDSUQSQSST=two",
            "ASPSESSIONIDSUQSQSST=one; ASPSESSIONIDSWRRSSTS=two; CookieKey=third",
            "ASPSESSIONIDSHORT=one",
            "ASPSESSIONIDlowercas=one",
            "ASPSESSIONIDSUQSQSST=one; ASPSESSIONIDSWRRSSTS=two; "
            "ASPSESSIONIDQWTSSQTS=three; ASPSESSIONIDQWTSSQTS=four",
            "ASPSESSIONIDSUQSQSST=one; ASPSESSIONIDSWRRSSTS=two; "
            "ASPSESSIONIDSWSSTSSS=three; ASPSESSIONIDSWSSTSSS=four",
            "ASPSESSIONIDSUQSQSST=one\r\nASPSESSIONIDSWRRSSTS=two",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cookies_from_header(bad)

    def test_cookie_jar_scoped_to_account_host(self):
        name = "ASPSESSIONIDSURRTSSS"
        jar = cookie_jar({name: "test-value"})
        account = urllib.request.Request("https://account.ncu.edu.tw/APSWIS_Q/a.asp")
        jar.add_cookie_header(account)
        self.assertEqual(account.get_header("Cookie"),
                         f"{name}=test-value")
        other = urllib.request.Request("https://portal.ncu.edu.tw/")
        jar.add_cookie_header(other)
        self.assertIsNone(other.get_header("Cookie"))

    def test_redirect_refuses_other_host(self):
        handler = SameHostRedirect()
        request = urllib.request.Request("https://account.ncu.edu.tw/original")
        with self.assertRaises(OffsiteRedirect):
            handler.redirect_request(request, None, 302, "Found", {},
                                     "https://portal.ncu.edu.tw/login?secret=hidden")

    def test_assessment_requires_more_than_http_200(self):
        path = "/APSWIS_Q/Dept_Add_Q/DA_SerBug_Menu_Q.asp"
        anonymous = ProbeResult(200, path, False, "same")
        self.assertIn("不能證明", assessment(ProbeResult(200, path, False, "same"), anonymous))
        self.assertIn("可能", assessment(ProbeResult(200, path, False, "different"), anonymous))
        self.assertIn("登入", assessment(ProbeResult(200, "/login", True, "other"), anonymous))

    def test_main_frame_target_is_resolved_from_outer_page(self):
        self.assertEqual(urllib.parse.urljoin(TARGET, "../ACCPR_Main_Q.asp"), MAIN_FRAME_TARGET)
        path = urllib.parse.urlsplit(MAIN_FRAME_TARGET).path
        anonymous = ProbeResult(200, path, False, "same")
        self.assertIn("不能證明", assessment(anonymous, anonymous, MAIN_FRAME_TARGET))
        self.assertIn("可能", assessment(
            ProbeResult(200, path, False, "different"), anonymous, MAIN_FRAME_TARGET,
        ))


if __name__ == "__main__":
    unittest.main()
