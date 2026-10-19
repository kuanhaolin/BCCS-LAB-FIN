import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import portal_login


class PortalLoginTests(unittest.TestCase):
    def test_configures_only_temporary_buster_profile_for_remote_speech(self):
        driver = Mock()
        driver.execute_async_script.side_effect = [
            {"storageVersion": portal_login.BUSTER_STORAGE_REVISION},
            dict(portal_login.BUSTER_SETTINGS),
        ]
        portal_login.configure_buster(driver)
        driver.get.assert_called_once_with(
            f"chrome-extension://{portal_login.EXTENSION_ID}/src/options/index.html")
        self.assertEqual(driver.execute_async_script.call_args.args[1], {
            "speechService": "managed",
            "enableManagedLocalServices": False,
            "enableManagedRemoteServices": True,
        })

    def test_buster_settings_must_be_confirmed_before_login(self):
        driver = Mock()
        driver.execute_async_script.side_effect = [
            {"storageVersion": portal_login.BUSTER_STORAGE_REVISION},
            {"speechService": "managed", "enableManagedLocalServices": True,
             "enableManagedRemoteServices": True},
        ]
        with self.assertRaisesRegex(RuntimeError, "無法確認 Buster"):
            portal_login.configure_buster(driver)

    def test_system_python_switches_to_selenium_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "bin/python"
            python.parent.mkdir()
            python.touch()
            with patch.object(portal_login, "VENV_PYTHON", python), \
                 patch.object(portal_login.importlib.util, "find_spec", return_value=None), \
                 patch.object(portal_login.sys, "argv", ["portal_login.py", "--env", "input.env"]), \
                 patch.object(portal_login.os, "execv") as execv:
                portal_login.ensure_selenium_python()
            execv.assert_called_once_with(str(python), [str(python),
                str(Path(portal_login.__file__).resolve()), "--env", "input.env"])

    def test_captcha_without_image_submits_directly(self):
        self.assertEqual(portal_login.captcha_decision("verified-token", False), "submit")
        self.assertEqual(portal_login.captcha_decision("verified-token", True), "submit")

    def test_captcha_uses_buster_only_for_visible_challenge(self):
        self.assertEqual(portal_login.captcha_decision("", False), "wait")
        self.assertEqual(portal_login.captcha_decision("", True), "buster")

    def test_finds_installed_buster_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension = (root / "Library/Application Support/Google/Chrome/Profile 1/Extensions"
                         / portal_login.EXTENSION_ID / "0.6.1_0")
            extension.mkdir(parents=True)
            (extension / "manifest.json").write_text(json.dumps({
                "homepage_url": "https://github.com/dessant/buster",
                "manifest_version": 3,
            }))
            with patch.object(portal_login.Path, "home", return_value=root):
                self.assertEqual(portal_login.find_buster_extension(), extension)

    def test_browser_session_uses_only_verified_cookie_names(self):
        cookies = [
            {"name": "portal", "value": "a", "domain": ".portal.ncu.edu.tw"},
            {"name": "SESSION", "value": "b", "domain": "portal.ncu.edu.tw"},
            {"name": "XSRF-TOKEN", "value": "c", "domain": "portal.ncu.edu.tw"},
            {"name": "other", "value": "ignored", "domain": "portal.ncu.edu.tw"},
            {"name": "portal", "value": "wrong", "domain": "other.example"},
        ]
        header = portal_login.cookie_header(cookies)
        self.assertEqual(header, "portal=a; SESSION=b; XSRF-TOKEN=c")

    def test_invalid_browser_session_is_rejected(self):
        cookies = [
            {"name": "portal", "value": "a", "domain": "portal.ncu.edu.tw"},
            {"name": "SESSION", "value": "b", "domain": "portal.ncu.edu.tw"},
            {"name": "XSRF-TOKEN", "value": "c\nInjected: yes", "domain": "portal.ncu.edu.tw"},
        ]
        with self.assertRaisesRegex(RuntimeError, "Cookie"):
            portal_login.cookie_header(cookies)

    def test_session_is_private_and_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text("old", encoding="utf-8")
            portal_login.save_session(path, "portal=a; SESSION=b; XSRF-TOKEN=c")
            self.assertEqual(json.loads(path.read_text())["portalCookie"],
                             "portal=a; SESSION=b; XSRF-TOKEN=c")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
