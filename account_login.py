#!/usr/bin/env python3
"""Log in to NCU accounting with OCR of the captcha displayed in this browser tab.

The captcha image is captured from the already-loaded page. Never fetch its URL
again: a second GET may show a different challenge for the same session.
"""

from __future__ import annotations

import argparse
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse

from PIL import Image, ImageOps

from account_session_probe import (
    MAIN_FRAME_TARGET, cookies_from_header, probe,
)
from portal_login import CHROME, CHROMEDRIVER, ROOT, VENV_PYTHON, load_env


LOGIN_URL = "https://account.ncu.edu.tw/APSWIS_Q/Login_L_Q.asp"
COOKIE_PATTERN = re.compile(r"ASPSESSIONID[A-Z0-9]{8}\Z")
CODE_PATTERN = re.compile(r"[0-9]{6}\Z")


def ensure_selenium_python() -> None:
    if importlib.util.find_spec("selenium") is not None:
        return
    if not VENV_PYTHON.is_file() or Path(sys.prefix) == VENV_PYTHON.parent.parent:
        raise RuntimeError("專用環境缺少 Selenium。")
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()),
                                *sys.argv[1:]])


def isolate_red_digits(png: bytes) -> bytes:
    """Remove the non-red background, enlarge the digits, and add white padding."""
    try:
        source = Image.open(io.BytesIO(png)).convert("RGB")
    except (OSError, ValueError) as exc:
        raise RuntimeError("驗證碼圖片無法讀取；沒有送出登入。") from exc
    if not (20 <= source.width <= 600 and 10 <= source.height <= 250):
        raise RuntimeError("驗證碼圖片尺寸與預期不同；沒有送出登入。")
    pixels = source.load()
    cleaned = Image.new("L", source.size, 255)
    output = cleaned.load()
    for y in range(source.height):
        for x in range(source.width):
            red, green, blue = pixels[x, y]
            if red > 105 and red > green + 35 and red > blue + 35:
                output[x, y] = 0
    if cleaned.getextrema()[0] == 255:
        raise RuntimeError("驗證碼沒有可辨識的紅色數字；沒有送出登入。")
    enlarged = cleaned.resize((source.width * 4, source.height * 4),
                              Image.Resampling.NEAREST)
    padded = ImageOps.expand(enlarged, border=24, fill=255)
    buffer = io.BytesIO()
    padded.save(buffer, format="PNG")
    return buffer.getvalue()


def recognize_code(png: bytes) -> str:
    """Require two OCR modes to agree on six digits; never retry a challenge."""
    executable = shutil.which("tesseract")
    if not executable:
        raise RuntimeError("找不到 Tesseract OCR；沒有送出登入。")
    processed = isolate_red_digits(png)
    readings: list[str] = []
    for mode in (6, 7, 8):
        try:
            result = subprocess.run(
                [executable, "stdin", "stdout", "--psm", str(mode), "-l", "eng",
                 "-c", "tessedit_char_whitelist=0123456789"],
                input=processed, capture_output=True, timeout=12, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("驗證碼辨識未完成；沒有送出登入。") from exc
        if result.returncode != 0:
            raise RuntimeError("驗證碼辨識失敗；沒有送出登入。")
        candidate = re.sub(r"\s+", "", result.stdout.decode("utf-8", "replace"))
        if CODE_PATTERN.fullmatch(candidate):
            readings.append(candidate)
    for code in readings:
        if readings.count(code) >= 2:
            return code
    raise RuntimeError("驗證碼辨識結果不一致；沒有送出登入。")


def accounting_cookie_header(cookies: list[dict]) -> str:
    """Keep every accounting ASPSESSIONID, including cookies from other paths."""
    selected: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if not isinstance(name, str) or not COOKIE_PATTERN.fullmatch(name):
            continue
        if str(cookie.get("domain", "")).lstrip(".") != "account.ncu.edu.tw":
            continue
        value = cookie.get("value")
        if not isinstance(value, str) or not value or any(ch in value for ch in ";\r\n"):
            raise RuntimeError("報帳 Session Cookie 格式不正確。")
        if name in selected and selected[name] != value:
            raise RuntimeError("報帳 Session Cookie 重複。")
        selected[name] = value
    if not selected:
        raise RuntimeError("登入後沒有取得報帳 Session Cookie。")
    header = "; ".join(f"{name}={value}" for name, value in selected.items())
    cookies_from_header(header)
    return header


def browser_accounting_cookies(driver: object) -> str:
    """Read the whole browser cookie store, not just the current page's path."""
    result = driver.execute_cdp_cmd("Storage.getCookies", {})
    cookies = result.get("cookies") if isinstance(result, dict) else None
    if not isinstance(cookies, list) or not all(isinstance(cookie, dict) for cookie in cookies):
        raise RuntimeError("無法讀取瀏覽器 Cookie；沒有保存 Session。")
    return accounting_cookie_header(cookies)


def browser_login(account: str, password: str) -> str:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        raise RuntimeError("專用環境缺少 Selenium。") from None
    if not CHROME.is_file() or not CHROMEDRIVER.is_file():
        raise RuntimeError("找不到相容的 Chrome 與 ChromeDriver。")

    with tempfile.TemporaryDirectory(prefix="ncu-account-login-") as profile:
        options = webdriver.ChromeOptions()
        options.binary_location = str(CHROME)
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        driver = None
        stage = "開啟報帳登入頁"
        try:
            driver = webdriver.Chrome(service=Service(str(CHROMEDRIVER)), options=options)
            driver.set_page_load_timeout(40)
            driver.get(LOGIN_URL)
            if urllib.parse.urlsplit(driver.current_url).path != "/APSWIS_Q/Login_L_Q.asp":
                raise RuntimeError("報帳登入頁位置與預期不同。")
            forms = [form for form in driver.find_elements(By.TAG_NAME, "form")
                     if all(form.find_elements(By.NAME, name)
                            for name in ("ID", "PWD", "CheckCode", "Enter"))]
            images = driver.find_elements(By.CSS_SELECTOR, 'img[src*="ValidCode_2.asp"]')
            if len(forms) != 1 or len(images) != 1:
                raise RuntimeError("報帳登入表單或驗證碼圖片結構已變更。")
            stage = "辨識當前驗證碼"
            image = images[0]
            for attempt in range(3):
                WebDriverWait(driver, 15).until(
                    lambda _: image.get_property("complete") and image.get_property("naturalWidth") > 0)
                try:
                    code = recognize_code(image.screenshot_as_png)
                    break
                except RuntimeError as exc:
                    if attempt == 2:
                        raise RuntimeError("連續三張驗證碼均無法可靠辨識；沒有送出登入。") from exc
                    # Reload the entire page so the next challenge and the form
                    # remain in the same browser session. Never fetch the image URL
                    # separately: that would invalidate the challenge we saw.
                    driver.refresh()
                    forms = [form for form in driver.find_elements(By.TAG_NAME, "form")
                             if all(form.find_elements(By.NAME, name)
                                    for name in ("ID", "PWD", "CheckCode", "Enter"))]
                    images = driver.find_elements(By.CSS_SELECTOR, 'img[src*="ValidCode_2.asp"]')
                    if len(forms) != 1 or len(images) != 1:
                        raise RuntimeError("重新載入後登入表單結構已變更；沒有送出登入。")
                    image = images[0]
            form = forms[0]
            form.find_element(By.NAME, "ID").send_keys(account)
            form.find_element(By.NAME, "PWD").send_keys(password)
            form.find_element(By.NAME, "CheckCode").send_keys(code)
            stage = "提交報帳登入"
            form.find_element(By.NAME, "Enter").click()
            WebDriverWait(driver, 25).until(
                lambda browser: browser.current_url != LOGIN_URL)
            landed_path = urllib.parse.urlsplit(driver.current_url).path
            if landed_path == "/APSWIS_Q/Login_L_Q.asp":
                raise RuntimeError("官方頁面仍停留在登入表單；請檢查帳密或驗證碼。")
            driver.get(MAIN_FRAME_TARGET)
            if urllib.parse.urlsplit(driver.current_url).path != "/APSWIS_Q/ACCPR_Main_Q.asp":
                raise RuntimeError("報帳系統尚未接受登入。")
            return browser_accounting_cookies(driver)
        except TimeoutException:
            raise RuntimeError(f"{stage}逾時；沒有保存 Session。") from None
        except WebDriverException:
            raise RuntimeError(f"{stage}失敗；沒有保存 Session。") from None
        finally:
            if driver is not None:
                driver.quit()


def verify_session(header: str) -> None:
    values = cookies_from_header(header)
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    anonymous = probe({}, context, MAIN_FRAME_TARGET)
    result = probe(values, context, MAIN_FRAME_TARGET)
    expected = urllib.parse.urlsplit(MAIN_FRAME_TARGET).path
    if (result.status != 200 or result.path != expected or result.password_field
            or result.offsite_host or (anonymous.status == result.status
                                       and anonymous.body_digest == result.body_digest)):
        raise RuntimeError("報帳 Session 未通過 MAIN 頁驗證；沒有保存。")


def save_session(path: Path, header: str) -> None:
    if path.is_symlink():
        raise RuntimeError("Session 檔不能是符號連結。")
    fd, temporary = tempfile.mkstemp(prefix=".account-session-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"accountCookie": header}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="辨識當前圖片驗證碼並登入 NCU 報帳系統")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--session", type=Path, default=ROOT / "session01.json")
    args = parser.parse_args()
    try:
        ensure_selenium_python()
        config = load_env(args.env)
        account, password = config.get("ACCOUNT_ID", ""), config.get("ACCOUNT_PW", "")
        if not account or not password:
            raise RuntimeError(".env 需要 ACCOUNT_ID、ACCOUNT_PW。")
        header = browser_login(account, password)
        verify_session(header)
        save_session(args.session, header)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"報帳登入未完成：{exc}")
        return 1
    print("報帳 Session 已驗證並保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
