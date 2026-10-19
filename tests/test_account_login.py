"""The accounting captcha is read from one displayed image, never a second URL GET."""

import subprocess
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import io

import account_login
from account_session_probe import ProbeResult


class AccountLoginTests(unittest.TestCase):
    def test_ocr_requires_two_matching_six_digit_readings(self):
        outputs = [b"757703\n", b"757703\n", b"757708\n"]
        image = Image.new("RGB", (120, 40), (255, 255, 255))
        image.putpixel((10, 10), (255, 0, 0))
        png = io.BytesIO()
        image.save(png, format="PNG")
        with patch("account_login.shutil.which", return_value="/usr/bin/tesseract"), \
             patch("account_login.subprocess.run",
                   side_effect=[Mock(returncode=0, stdout=output) for output in outputs]) as run:
            self.assertEqual(account_login.recognize_code(png.getvalue()), "757703")
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(call.kwargs["input"] == run.call_args_list[0].kwargs["input"]
                            for call in run.call_args_list))

    def test_ocr_ambiguity_stops_without_submitting(self):
        outputs = [b"123456\n", b"123457\n", b"garbage\n"]
        image = Image.new("RGB", (120, 40), (255, 255, 255))
        image.putpixel((10, 10), (255, 0, 0))
        png = io.BytesIO()
        image.save(png, format="PNG")
        with patch("account_login.shutil.which", return_value="/usr/bin/tesseract"), \
             patch("account_login.subprocess.run",
                   side_effect=[Mock(returncode=0, stdout=output) for output in outputs]):
            with self.assertRaisesRegex(RuntimeError, "不一致"):
                account_login.recognize_code(png.getvalue())

    def test_ocr_timeout_stops(self):
        image = Image.new("RGB", (120, 40), (255, 255, 255))
        image.putpixel((10, 10), (255, 0, 0))
        png = io.BytesIO()
        image.save(png, format="PNG")
        with patch("account_login.shutil.which", return_value="/usr/bin/tesseract"), \
             patch("account_login.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("tesseract", 12)):
            with self.assertRaisesRegex(RuntimeError, "沒有送出登入"):
                account_login.recognize_code(png.getvalue())

    def test_only_account_host_asp_session_cookies_are_returned(self):
        header = account_login.accounting_cookie_header([
            {"name": "ASPSESSIONIDSWRRSSTS", "value": "one",
             "domain": "account.ncu.edu.tw"},
            {"name": "ASPSESSIONIDSUQSQSST", "value": "two",
             "domain": ".account.ncu.edu.tw"},
            {"name": "ASPSESSIONIDSWRRSSTS", "value": "wrong",
             "domain": "other.example"},
            {"name": "CookieKey", "value": "ignored",
             "domain": "account.ncu.edu.tw"},
        ])
        self.assertEqual(header, "ASPSESSIONIDSWRRSSTS=one; ASPSESSIONIDSUQSQSST=two")

    def test_reads_every_account_session_from_browser_store_across_paths(self):
        names = (
            "ASPSESSIONIDSWRRSSTS", "ASPSESSIONIDSUQSQSST",
            "ASPSESSIONIDQWTSSQTS", "ASPSESSIONIDSWSSTSSS",
            "ASPSESSIONIDSURRTSSS", "ASPSESSIONIDQUSTSRST",
        )
        cookies = [
            {"name": name, "value": f"value{i}", "domain": "account.ncu.edu.tw",
             "path": "/APSWIS_Q/Dept_Add_Q" if i % 2 else "/APSWIS_Q"}
            for i, name in enumerate(names)
        ]
        cookies.append({"name": "ASPSESSIONIDQWTSSQTS", "value": "not-account",
                        "domain": "cis.ncu.edu.tw", "path": "/"})
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"cookies": cookies}

        header = account_login.browser_accounting_cookies(driver)

        driver.execute_cdp_cmd.assert_called_once_with("Storage.getCookies", {})
        self.assertEqual(len(header.split("; ")), 6)
        self.assertEqual(header, "; ".join(
            f"{name}=value{i}" for i, name in enumerate(names)))

    def test_browser_store_does_not_silently_drop_conflicting_session_names(self):
        name = "ASPSESSIONIDSWRRSSTS"
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"cookies": [
            {"name": name, "value": "one", "domain": "account.ncu.edu.tw", "path": "/"},
            {"name": name, "value": "two", "domain": "account.ncu.edu.tw",
             "path": "/APSWIS_Q"},
        ]}
        with self.assertRaisesRegex(RuntimeError, "重複"):
            account_login.browser_accounting_cookies(driver)

    def test_browser_store_malformed_response_fails_without_fallback(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"cookies": None}
        with self.assertRaisesRegex(RuntimeError, "沒有保存"):
            account_login.browser_accounting_cookies(driver)

    def test_main_page_distinguishes_authenticated_session_from_anonymous(self):
        path = "/APSWIS_Q/ACCPR_Main_Q.asp"
        header = "ASPSESSIONIDSUQTTSSS=test"
        responses = [ProbeResult(404, path, False, "anonymous"),
                     ProbeResult(200, path, False, "authenticated")]
        with patch("account_login.probe", side_effect=responses):
            account_login.verify_session(header)
        with patch("account_login.probe", side_effect=[
            ProbeResult(404, path, False, "anonymous"),
            ProbeResult(404, path, False, "anonymous"),
        ]):
            with self.assertRaisesRegex(RuntimeError, "MAIN 頁驗證"):
                account_login.verify_session(header)


if __name__ == "__main__":
    unittest.main()
