#!/usr/bin/env python3
"""Log in to NCU Portal using the installed Buster extension and Selenium."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
CACHE = Path.home() / "Library/Caches/ncu-portal"
CHROME = (CACHE / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
          "Google Chrome for Testing")
CHROMEDRIVER = CACHE / "chromedriver-mac-arm64/chromedriver"
VENV_PYTHON = CACHE / "selenium-venv/bin/python"
EXTENSION_ID = "mpbjkejclgfgadiemmefgebjfooflfhl"
BUSTER_STORAGE_REVISION = "20260618183413_configure_managed_services"
BUSTER_SETTINGS = {
    "speechService": "managed",
    "enableManagedLocalServices": False,
    "enableManagedRemoteServices": True,
}
COOKIE_NAMES = ("portal", "SESSION", "XSRF-TOKEN")
LOGIN_URL = "https://portal.ncu.edu.tw/login"
MENU_URL = "https://portal.ncu.edu.tw/backends/menu"


def captcha_decision(token: str, image_challenge_visible: bool) -> str:
    if token:
        return "submit"
    if image_challenge_visible:
        return "buster"
    return "wait"


def ensure_selenium_python() -> None:
    if importlib.util.find_spec("selenium") is not None:
        return
    if not VENV_PYTHON.is_file() or Path(sys.prefix) == VENV_PYTHON.parent.parent:
        raise RuntimeError("尚未安裝 Selenium；請檢查專用虛擬環境。")
    # Replace this process before reading .env. Account, password and cookies
    # never appear in the new process's command-line arguments.
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[name.strip()] = value
    return result


def find_buster_extension() -> Path:
    profiles = Path.home() / "Library/Application Support/Google/Chrome"
    candidates = []
    for manifest in profiles.glob(f"*/Extensions/{EXTENSION_ID}/*/manifest.json"):
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (info.get("homepage_url") == "https://github.com/dessant/buster"
                and info.get("manifest_version") == 3):
            candidates.append(manifest.parent)
    if not candidates:
        raise RuntimeError("找不到 Chrome 已安裝的 Buster 擴充功能。")
    return max(candidates, key=lambda path: tuple(
        int(part) for part in path.name.split("_")[0].split(".")))


def cookie_header(cookies: list[dict]) -> str:
    selected: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if name not in COOKIE_NAMES:
            continue
        if str(cookie.get("domain", "")).lstrip(".") != "portal.ncu.edu.tw":
            continue
        value = cookie.get("value")
        if (not isinstance(value, str) or not value or ";" in value
                or "\r" in value or "\n" in value):
            raise RuntimeError("Portal Session Cookie 格式不正確。")
        if name in selected and selected[name] != value:
            raise RuntimeError("Portal Session Cookie 重複。")
        selected[name] = value
    if any(name not in selected for name in COOKIE_NAMES):
        raise RuntimeError("登入後缺少 Portal Session Cookie。")
    return "; ".join(f"{name}={selected[name]}" for name in COOKIE_NAMES)


def configure_buster(driver) -> None:
    """Configure only the disposable automation profile, never the user's Chrome."""
    driver.get(f"chrome-extension://{EXTENSION_ID}/src/options/index.html")
    driver.set_script_timeout(10)
    for _ in range(40):
        state = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            if (!globalThis.chrome?.storage?.local) return done(null);
            chrome.storage.local.get(['storageVersion'], done);
        """)
        if isinstance(state, dict) and state.get("storageVersion") == BUSTER_STORAGE_REVISION:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("Buster 設定尚未初始化；沒有嘗試登入。")

    settings = driver.execute_async_script("""
        const settings = arguments[0];
        const done = arguments[arguments.length - 1];
        chrome.storage.local.set(settings, () => {
            if (chrome.runtime.lastError) return done(null);
            chrome.storage.local.get(Object.keys(settings), done);
        });
    """, BUSTER_SETTINGS)
    if (not isinstance(settings, dict) or
            settings.get("speechService") != "managed" or
            settings.get("enableManagedLocalServices") is not False or
            settings.get("enableManagedRemoteServices") is not True):
        raise RuntimeError("無法確認 Buster 已關閉 Local、開啟 Remote；沒有嘗試登入。")


def browser_login(account: str, password: str, extension: Path) -> str:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver import ActionChains
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        raise RuntimeError("專用環境缺少 Selenium，請檢查安裝狀態。") from None
    if not CHROME.is_file() or not CHROMEDRIVER.is_file():
        raise RuntimeError("找不到相同版本的 Chrome for Testing 與 ChromeDriver。")
    if not (extension / "manifest.json").is_file():
        raise RuntimeError("Buster 擴充功能目錄不存在。")

    with tempfile.TemporaryDirectory(prefix="ncu-portal-buster-") as profile:
        options = webdriver.ChromeOptions()
        options.binary_location = str(CHROME)
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument(f"--disable-extensions-except={extension}")
        options.add_argument(f"--load-extension={extension}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--window-size=1200,900")
        driver = None
        stage = "開啟瀏覽器"
        try:
            driver = webdriver.Chrome(service=Service(str(CHROMEDRIVER)), options=options)
            driver.set_page_load_timeout(40)
            stage = "設定 Buster"
            configure_buster(driver)
            print("Buster 已設定為 Local 關、Remote 開。", flush=True)
            stage = "開啟 Portal"
            driver.get(LOGIN_URL)
            wait = WebDriverWait(driver, 30)
            forms = driver.find_elements(By.CSS_SELECTOR, 'form[action="/login"]')
            if len(forms) != 1 or urlparse(forms[0].get_attribute("action")).path != "/login":
                raise RuntimeError("Portal 登入表單結構已變更。")
            form = forms[0]
            username = form.find_element(By.NAME, "username")
            password_field = form.find_element(By.NAME, "password")
            if not form.find_elements(By.CSS_SELECTOR, ".g-recaptcha"):
                raise RuntimeError("Portal 驗證框結構已變更。")
            username.send_keys(account)
            password_field.send_keys(password)

            stage = "點選驗證框"
            anchor = wait.until(lambda browser: next((frame for frame in
                browser.find_elements(By.CSS_SELECTOR, 'iframe[src*="/recaptcha/api2/anchor"]')
                if frame.is_displayed()), None))
            driver.switch_to.frame(anchor)
            wait.until(lambda browser: browser.find_element(By.ID, "recaptcha-anchor")).click()
            driver.switch_to.default_content()

            stage = "等待 reCAPTCHA 判斷"
            def token_or_challenge(browser):
                token = browser.execute_script(
                    "return document.querySelector('[name=\"g-recaptcha-response\"]')?.value || ''")
                challenge = next((frame for frame in browser.find_elements(
                    By.CSS_SELECTOR, 'iframe[src*="/recaptcha/api2/bframe"]')
                    if frame.is_displayed()), None)
                decision = captcha_decision(token, challenge is not None)
                return (decision, challenge) if decision != "wait" else False

            decision, challenge = WebDriverWait(driver, 25).until(token_or_challenge)
            buster_used = False
            if decision == "buster":
                # The checkbox may finish verification just as the challenge opens.
                token = driver.execute_script(
                    "return document.querySelector('[name=\"g-recaptcha-response\"]')?.value || ''")
                if not token:
                    buster_used = True
                    print("出現圖片驗證，正在使用 Buster。", flush=True)
                    stage = "等待 Buster 語音驗證"
                    driver.switch_to.frame(challenge)
                    # Buster 3.4.0 keeps its button in a closed shadow root.
                    # Selenium's real pointer click on the host reaches it.
                    holder = WebDriverWait(driver, 20).until(lambda browser: next((element
                        for element in browser.find_elements(By.CSS_SELECTOR,
                            ".help-button-holder")
                        if element.is_displayed() and not browser.find_elements(
                            By.CSS_SELECTOR, "#recaptcha-help-button")), None))
                    ActionChains(driver).move_to_element(holder).click().perform()
                    driver.switch_to.default_content()
                    print("已點選 Buster；正在檢查語音挑戰是否開啟。", flush=True)

                    def buster_progress(browser):
                        if browser.current_url != LOGIN_URL or browser.execute_script(
                            "return !!document.querySelector('[name=\"g-recaptcha-response\"]')?.value"):
                            return "solved"
                        challenge_frames = browser.find_elements(By.CSS_SELECTOR,
                            'iframe[src*="/recaptcha/api2/bframe"]')
                        for frame in challenge_frames:
                            if not frame.is_displayed():
                                continue
                            try:
                                browser.switch_to.frame(frame)
                                return browser.execute_script("""
                                    if (document.querySelector('.rc-doscaptcha-body')) return 'blocked';
                                    if (document.querySelector('#audio-response')?.value) return 'transcribed';
                                    if (document.querySelector('audio#audio-source')) return 'audio';
                                    return 'waiting';
                                """)
                            finally:
                                browser.switch_to.default_content()
                        return "waiting"

                    try:
                        progress = WebDriverWait(driver, 20).until(lambda browser:
                            (state if (state := buster_progress(browser)) != "waiting" else False))
                    except TimeoutException:
                        raise RuntimeError("已點選 Buster，但未開啟語音挑戰；請查看瀏覽器中的提示。") from None
                    if progress == "blocked":
                        time.sleep(100);
                        raise RuntimeError("官方目前不提供語音挑戰；Buster 無法解題。")
                    if progress == "audio":
                        print("語音挑戰已開啟，等待 Buster 辨識。", flush=True)
                    if progress == "transcribed":
                        print("Buster 已填入語音辨識結果，等待官方驗證。", flush=True)
            else:
                print("沒有圖片驗證，直接提交登入。", flush=True)

            stage = "等待驗證結果"
            reported_transcription = progress == "transcribed" if buster_used else False

            def verification_ready(browser):
                nonlocal reported_transcription
                if browser.current_url != LOGIN_URL or browser.execute_script(
                    "return !!document.querySelector('[name=\"g-recaptcha-response\"]')?.value"):
                    return True
                if buster_used:
                    state = buster_progress(browser)
                    if state == "blocked":
                        raise RuntimeError("官方目前不提供語音挑戰；Buster 無法解題。")
                    if state == "transcribed" and not reported_transcription:
                        print("Buster 已填入語音辨識結果，等待官方驗證。", flush=True)
                        reported_transcription = True
                return False

            try:
                WebDriverWait(driver, 90, poll_frequency=2).until(verification_ready)
            except TimeoutException:
                if buster_used:
                    detail = "官方未接受辨識結果" if reported_transcription else "Buster 未完成語音辨識"
                    raise RuntimeError(f"{detail}；沒有提交登入。") from None
                raise
            if driver.current_url == LOGIN_URL:
                stage = "提交登入"
                submit = form.find_elements(By.CSS_SELECTOR,
                    'button[type="submit"], input[type="submit"]')
                if not submit:
                    raise RuntimeError("找不到 Portal 登入按鈕。")
                submit[0].click()
                WebDriverWait(driver, 30).until(
                    lambda browser: browser.current_url != LOGIN_URL)

            stage = "確認 Session"
            driver.get(MENU_URL)
            if driver.current_url != MENU_URL:
                raise RuntimeError("Portal 尚未接受登入。")
            try:
                menu = json.loads(driver.find_element(By.TAG_NAME, "body").text)
            except ValueError:
                raise RuntimeError("Portal 選單不是有效的登入回應。") from None
            if not isinstance(menu, (dict, list)):
                raise RuntimeError("Portal 選單不是有效的登入回應。")
            return cookie_header(driver.get_cookies())
        except TimeoutException:
            raise RuntimeError(f"{stage}逾時；可能是音訊驗證未開放或未完成。") from None
        except WebDriverException:
            raise RuntimeError(f"{stage}失敗；請檢查 Chrome／Buster 狀態。") from None
        finally:
            if driver is not None:
                driver.quit()


def save_session(path: Path, header: str) -> None:
    if path.is_symlink():
        raise RuntimeError("session.json 不能是符號連結。")
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"portalCookie": header}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="透過 Buster 語音驗證登入 NCU Portal")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--session", type=Path, default=ROOT / "session.json")
    args = parser.parse_args()
    try:
        ensure_selenium_python()
        config = load_env(args.env)
        account = config.get("PORTAL_ID", "")
        password = config.get("PORTAL_PW", "")
        if not account or not password:
            raise RuntimeError(".env 需要 PORTAL_ID、PORTAL_PW。")
        extension = find_buster_extension()
        print("正在開啟 Chrome，使用 Buster 嘗試一次語音驗證。", flush=True)
        session = browser_login(account, password, extension)
        save_session(args.session, session)
    except RuntimeError as exc:
        print(f"登入失敗：{exc}")
        return 1
    except (OSError, ValueError):
        print("登入失敗：瀏覽器或檔案讀寫錯誤。")
        return 1
    print("Portal 登入成功，session.json 已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
