#!/usr/bin/env python3
"""Local-only NCU form workbench for HumanSys, accounting, and research workflows.

HumanSys and ResearchSys share the saved Portal Cookie; accounting uses its own Cookie.
The authenticated HTTP cookie jar remains in memory for at most 15 minutes.
The web server listens on 127.0.0.1 only and deliberately suppresses access logs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from email.parser import BytesParser
from email.policy import default as email_policy
import html
from html.parser import HTMLParser
import hashlib
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from humansys_apply_preview import MAX_ATTACHMENT_BYTES, prepare_record
from humansys_business_draft import save_draft
from humansys_business_report import DISPLAY_FIELDS, fetch_report, save_report_pdf
from humansys_business_list import (
    FIELDS, LIST_URL, list_html_diagnostics, parse_list, rows_for_start_date,
    selected_submit_form, validate_roc_date,
)
from humansys_sso_probe import (
    DEFAULT_TARGET, DEFAULT_USER_AGENT, CookieTracker, FormParser, RedirectTracker,
    find_consent_submission, find_sso_form, open_page, parse_cookie_header,
    seed_cookie_jar,
)
from account_session_probe import (
    MAIN_FRAME_TARGET, cookies_from_header as account_cookies_from_header,
    OffsiteRedirect, probe as probe_account_session,
)
from account_direct_save import prepare_save, send_save_once
from account_personnel_save import prepare_personnel_save
from account_purchase_list import PurchaseRow, fetch_purchase_list
from account_print_pdf import find_purchase, save_purchase_pdf
from research_budget import (bootstrap_research_from_portal, fetch_projects,
                             save_budget_pdf)
from research_human_a import HumanPerson, fetch_human_people, save_human_a_pdf
from appropriation_list import fetch_list as fetch_appropriation_list, save_pdf as save_appropriation_pdf


INPUT_JSON = Path(__file__).with_name("input.json")
ACCOUNT_INPUT_JSON = Path(__file__).with_name("input01.json")
PERSONNEL_INPUT_JSON = Path(__file__).with_name("input03.json")
APPROPRIATION_INPUT_JSON = Path(__file__).with_name("input02.json")
SESSION_JSON = Path(__file__).with_name("session.json")
ACCOUNT_SESSION_JSON = Path(__file__).with_name("session01.json")
PASSWORD_JSON = Path(__file__).with_name("p@ssw0rd.json")
SESSION_TTL = 15 * 60
MAX_POST_BYTES = 6 * 1024 * 1024
APP_VERSION = "2026-10-19 修正版 v81"
APPROPRIATION_URL = "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_Q.do?isShowAll=true"
APPROPRIATION_CREATE_URL = "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_IPre.do"
APPROPRIATION_ADD_URL = "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_Sub.do"
APPROPRIATION_SUBMIT_URL = "https://cis.ncu.edu.tw/Appropriation/user/payloadlist_IuseO.do"
PORTAL_MENU_URL = "https://portal.ncu.edu.tw/backends/menu"
ACCOUNT_RETRY_GUARD_SECONDS = 15 * 60
SESSION_COOKIE_NAMES = ("portal", "SESSION", "XSRF-TOKEN")
PORTAL_LOGIN_TIMEOUT = 240
ACCOUNT_LOGIN_TIMEOUT = 120


@dataclass
class LocalSession:
    opener: urllib.request.OpenerDirector
    csrf: str
    expires: float


SESSIONS: dict[str, LocalSession] = {}
ACCOUNT_SESSIONS: dict[str, tuple[dict[str, str], float]] = {}
RESEARCH_SESSIONS: dict[str, LocalSession] = {}
APPROPRIATION_SESSIONS: dict[str, LocalSession] = {}
APPROPRIATION_ADD_ATTEMPTS: dict[tuple[str, str], float] = {}
ACCOUNT_SAVE_ATTEMPTS: dict[tuple[str, str], float] = {}
START_CSRF = secrets.token_urlsafe(32)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def session_saved_status(connected: bool) -> str:
    return '<p class="ok">Session 已保存。</p>' if connected else ""


def load_defaults() -> dict[str, object]:
    try:
        data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 input.json：{exc}") from exc
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("input.json 必須是一筆資料。")
    return data


APPROPRIATION_FIELDS = (
    "pay_dept_no", "plan_sno", "remarks", "title", "name",
    "staff_personal_no", "list_date", "ought_money",
    "health2_money", "actual_money", "postoffice_no1", "postoffice_no2",
    "address", "memo",
)
APPROPRIATION_CHECKBOXES = ("isAccumulate", "is_techfund")


def load_appropriation_defaults() -> dict[str, str | bool]:
    try:
        data = json.loads(APPROPRIATION_INPUT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 input02.json：{exc}") from exc
    if (not isinstance(data, dict)
            or any(not isinstance(data.get(name), str) for name in APPROPRIATION_FIELDS)
            or any(not isinstance(data.get(name), bool) for name in APPROPRIATION_CHECKBOXES)):
        raise ValueError("input02.json 欄位格式不正確。")
    return {name: data[name] for name in (*APPROPRIATION_FIELDS, *APPROPRIATION_CHECKBOXES)}


class _AppropriationFormProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.actions: list[str] = []
        self.names: set[str] = set()
        self.pay_dept_options: set[str] = set()
        self._pay_dept_select = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.actions.append(values.get("action") or "")
        if tag == "select":
            self._pay_dept_select = values.get("name") == "pay_dept_no"
        if tag == "option" and self._pay_dept_select and values.get("value"):
            self.pay_dept_options.add(values["value"] or "")
        if tag in ("input", "textarea", "select") and values.get("name"):
            self.names.add(values["name"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._pay_dept_select = False


def appropriation_form_status(session: LocalSession) -> str:
    response = open_page(session.opener, APPROPRIATION_CREATE_URL, DEFAULT_USER_AGENT)
    final = urllib.parse.urlsplit(response.geturl())
    if (response.getcode() != 200 or final.scheme != "https"
            or final.hostname != "cis.ncu.edu.tw"
            or not final.path.startswith("/Appropriation/")):
        return "unknown"
    body = response.read(2_000_000)
    probe = _AppropriationFormProbe()
    probe.feed(body.decode("utf-8", "replace"))
    actions = {urllib.parse.urljoin(response.geturl(), action) for action in probe.actions}
    if "https://cis.ncu.edu.tw/Appropriation/login.do" in actions:
        return "login"
    if ("https://cis.ncu.edu.tw/Appropriation/user/payloadlist_Sub.do" in actions
            and set(APPROPRIATION_FIELDS).issubset(probe.names)):
        return "form"
    return "unknown"


def appropriation_add_payload(opener: urllib.request.OpenerDirector,
                              values: dict[str, str]) -> bytes:
    """Validate the live official add form and build its browser-equivalent POST body."""
    response = open_page(opener, APPROPRIATION_CREATE_URL, DEFAULT_USER_AGENT)
    if response.getcode() != 200 or response.geturl() != APPROPRIATION_CREATE_URL:
        raise ValueError("無法讀取官方新增表單；沒有加入。")
    body = response.read(2_000_000).decode("utf-8", "replace")
    forms = FormParser()
    forms.feed(body)
    matches = [form for form in forms.forms
               if urllib.parse.urljoin(APPROPRIATION_CREATE_URL, str(form["action"]))
               == APPROPRIATION_ADD_URL and form["method"] == "post"]
    if len(matches) != 1 or not any(button.get("label") == "加入"
                                    for button in matches[0]["buttons"]):
        raise ValueError("找不到唯一的官方加入表單；沒有加入。")
    controls = {control["name"]: control for control in matches[0]["controls"]}
    expected = {"action": "ADD", "calhealth": "I", "post_checked": "false"}
    if (any(controls.get(name, {}).get("value") != value
            for name, value in expected.items())
            or not {"isAccumulate_hidden", "is_techfund_hidden", "Submit"}.issubset(controls)):
        raise ValueError("官方加入表單欄位已變更；沒有加入。")
    flow = (controls.get("from", {}).get("value"),
            controls.get("index", {}).get("value"))
    if flow not in (("I", "-1"), ("IuseO", "0")):
        raise ValueError("官方新增流程標記不符；沒有加入。")
    probe = _AppropriationFormProbe()
    probe.feed(body)
    if not set(APPROPRIATION_FIELDS).issubset(probe.names):
        raise ValueError("官方加入表單缺少欄位；沒有加入。")
    if values["pay_dept_no"] not in probe.pay_dept_options:
        raise ValueError("報帳單位不在官方下拉選單；沒有加入。")
    pairs = [(name, values[name]) for name in APPROPRIATION_FIELDS]
    for name in APPROPRIATION_CHECKBOXES:
        checked = values.get(name) == "on"
        if checked:
            pairs.append((name, "M1"))
        pairs.append((name + "_hidden", "M1" if checked else "M2"))
    pairs.extend((*expected.items(), ("from", flow[0]), ("index", flow[1]),
                  ("Submit", "加入")))
    return urllib.parse.urlencode(pairs).encode("utf-8")


def validate_appropriation_values(values: dict[str, str]) -> None:
    if any(name not in values for name in APPROPRIATION_FIELDS):
        raise ValueError("主持人費欄位不完整；沒有加入。")
    if (not re.fullmatch(r"[A-Z0-9]{3,4}", values["pay_dept_no"])
            or not re.fullmatch(r"[A-Za-z0-9-]{3,30}", values["plan_sno"])):
        raise ValueError("報帳單位或計畫流水號格式不正確；沒有加入。")
    if (not values["title"].strip() or not values["name"].strip()
            or not re.fullmatch(r"[A-Za-z][0-9]{9}", values["staff_personal_no"])):
        raise ValueError("職稱、姓名或身分證字號不完整；沒有加入。")
    month = values["list_date"]
    if not re.fullmatch(r"[0-9]{5}", month) or not 1 <= int(month[-2:]) <= 12:
        raise ValueError("年月請填民國五碼；沒有加入。")
    for name in ("ought_money", "health2_money", "actual_money"):
        if not re.fullmatch(r"[0-9]{1,10}", values[name]):
            raise ValueError("金額欄位必須是非負整數；沒有加入。")
    if int(values["ought_money"]) <= 0 or int(values["actual_money"]) <= 0:
        raise ValueError("應領與實發金額必須大於 0；沒有加入。")
    if (len(values["remarks"]) > 255 or len(values["memo"]) > 255
            or any(len(values[name]) > 100 for name in APPROPRIATION_FIELDS
                   if name not in ("remarks", "memo"))):
        raise ValueError("欄位文字過長；沒有加入。")
    if any(values.get(name, "") not in ("", "on") for name in APPROPRIATION_CHECKBOXES):
        raise ValueError("勾選欄位格式不正確；沒有加入。")


def _appropriation_submit_url(values: dict[str, str]) -> str:
    """Build the observed official final-submit URL without requiring an HTML button."""
    query = urllib.parse.urlencode((
        ("a", ""),
        ("pay_dept_no", values["pay_dept_no"]),
        ("plan_sno", values["plan_sno"]),
        ("remarks", values["remarks"]),
        ("isAccumulate", "M1" if values.get("isAccumulate") == "on" else "M2"),
        ("is_techfund", "M1" if values.get("is_techfund") == "on" else "M2"),
        ("calhealth", "I"),
    ))
    return APPROPRIATION_SUBMIT_URL + "?" + query


def send_appropriation_once(opener: urllib.request.OpenerDirector,
                            payload: bytes, values: dict[str, str],
                            previous_numbers: set[str], progress: list[str]) -> str | None:
    """Perform one official add, directly submit once, then read back the list."""
    progress[0] = "adding"
    response = open_page(opener, APPROPRIATION_ADD_URL, DEFAULT_USER_AGENT,
                         data=payload, referer=APPROPRIATION_CREATE_URL)
    progress[0] = "add_response"
    add_url = urllib.parse.urlsplit(response.geturl())
    body = response.read(2_000_001)
    if (response.getcode() != 200 or add_url.scheme != "https"
            or add_url.hostname != "cis.ncu.edu.tw"
            or add_url.path != "/Appropriation/user/payloadlist_Sub.do"
            or "html" not in response.headers.get("Content-Type", "").lower()
            or len(body) > 2_000_000):
        raise ValueError("官方加入回應不符；請檢查是否有待送出資料，勿直接重試。")
    progress[0] = "added"
    submit_url = _appropriation_submit_url(values)
    progress[0] = "submitting"
    response = open_page(opener, submit_url, DEFAULT_USER_AGENT,
                         referer=urllib.parse.urlunsplit(add_url))
    progress[0] = "submit_response"
    final = urllib.parse.urlsplit(response.geturl())
    body = response.read(2_000_001)
    if (response.getcode() != 200 or final.scheme != "https"
            or final.hostname != "cis.ncu.edu.tw"
            or not final.path.startswith("/Appropriation/")
            or "html" not in response.headers.get("Content-Type", "").lower()
            or len(body) > 2_000_000):
        raise ValueError("官方送出回應不符；請到清單確認結果，勿直接重試。")
    progress[0] = "submitted"
    after, _ = fetch_appropriation_list(opener, 1)
    new_rows = [row for row in after if row.number not in previous_numbers
                and row.plan_number == values["plan_sno"]]
    return new_rows[0].number if len(new_rows) == 1 else None


class _AppropriationRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != "https" or target.netloc not in
                ("portal.ncu.edu.tw", "cis.ncu.edu.tw")
                or (target.hostname == "cis.ncu.edu.tw"
                    and not target.path.startswith("/Appropriation/"))
                or self.hops >= 15):
            raise ValueError("撥帳系統導向非預期頁面；沒有連線。")
        self.hops += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def bootstrap_appropriation_from_portal(portal_cookie: str) -> urllib.request.OpenerDirector:
    """Use a fresh Portal menu token; never reuse a pasted portalLogin URL."""
    values = parse_cookie_header(portal_cookie)
    cookie_values = dict(values)
    if any(not cookie_values.get(name) for name in SESSION_COOKIE_NAMES):
        raise ValueError("請先更新 Portal Session。")
    jar = http.cookiejar.CookieJar()
    seed_cookie_jar(jar, "portal.ncu.edu.tw", values)
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    opener = urllib.request.build_opener(
        CookieTracker(jar), _AppropriationRedirect(),
        urllib.request.HTTPSHandler(context=context))
    response = open_page(opener, PORTAL_MENU_URL, DEFAULT_USER_AGENT)
    if response.getcode() != 200 or response.geturl() != PORTAL_MENU_URL:
        raise ValueError("無法讀取 Portal 系統選單；沒有連線。")
    menu = json.loads(response.read(2_000_000))
    matches: list[dict[str, object]] = []

    def find(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("data"), dict) and value["data"].get("id") == 28:
                matches.append(value)
            for child in value.values():
                find(child)
        elif isinstance(value, list):
            for child in value:
                find(child)

    find(menu)
    if len(matches) != 1 or not isinstance(matches[0].get("url"), str):
        raise ValueError("Portal 選單中找不到唯一的撥帳系統入口；沒有連線。")
    launch = urllib.parse.urljoin("https://portal.ncu.edu.tw/", matches[0]["url"])
    parsed = urllib.parse.urlsplit(launch)
    query = urllib.parse.parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != "portal.ncu.edu.tw"
            or parsed.path != "/system/46" or set(query) != {"token"}
            or len(query["token"]) != 1 or not query["token"][0]):
        raise ValueError("Portal 撥帳入口格式不符；沒有連線。")
    response = open_page(opener, launch, DEFAULT_USER_AGENT,
                         referer="https://portal.ncu.edu.tw/")
    target = urllib.parse.urlsplit(response.geturl())
    response.read(2_000_000)
    if (response.getcode() != 200 or target.hostname != "cis.ncu.edu.tw"
            or target.path != "/Appropriation/portalLogin.do"
            or not any(cookie.name == "JSESSIONID"
                       and cookie.domain.lstrip(".") == "cis.ncu.edu.tw"
                       and cookie.path.startswith("/Appropriation") for cookie in jar)):
        raise ValueError("Portal 未完成撥帳系統登入；沒有連線。")
    return opener


def default_filenames(data: dict[str, object]) -> list[str]:
    files = data.get("proofFile", [])
    if not isinstance(files, list):
        return []
    return [item["filename"] for item in files
            if isinstance(item, dict) and isinstance(item.get("filename"), str)]


def save_json(path: Path, value: object, private: bool = False) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600 if private else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_password_memos() -> list[dict[str, str]]:
    if PASSWORD_JSON.is_symlink():
        raise ValueError("備忘錄檔案不能是符號連結。")
    try:
        if PASSWORD_JSON.stat().st_size > 1024 * 1024:
            raise ValueError("備忘錄檔案超過 1 MB，已停止讀取。")
        data = json.loads(PASSWORD_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("無法讀取 p@ssw0rd.json；沒有修改檔案。") from exc
    if not isinstance(data, list) or len(data) > 200:
        raise ValueError("p@ssw0rd.json 格式不正確；沒有修改檔案。")
    memos: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("p@ssw0rd.json 格式不正確；沒有修改檔案。")
        if set(item) == {"id", "password"}:
            item = {"system": "", "account": item["id"], "password": item["password"]}
        if (set(item) != {"system", "account", "password"}
                or any(not isinstance(value, str) for value in item.values())
                or not item["account"]):
            raise ValueError("p@ssw0rd.json 格式不正確；沒有修改檔案。")
        memos.append(item)
    keys = [(item["system"], item["account"]) for item in memos]
    if len(keys) != len(set(keys)):
        raise ValueError("p@ssw0rd.json 有重複的系統與帳號；沒有修改檔案。")
    return memos


def validate_memo_label(value: str, label: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label}必須為 1–100 字，且不可包含控制字元。")
    return value


def validate_memo_password(value: str) -> str:
    if not value or len(value) > 500 or any(char in value for char in "\r\n\x00"):
        raise ValueError("密碼必須為 1–500 字的單行文字。")
    return value


def update_password_memos(operation: str, fields: dict[str, str]) -> None:
    memos = load_password_memos()
    if operation == "add":
        system = validate_memo_label(fields.get("system", ""), "系統")
        account = validate_memo_label(fields.get("account", ""), "帳號")
        password = validate_memo_password(fields.get("password", ""))
        if any(item["system"] == system and item["account"] == account for item in memos):
            raise ValueError("這個系統與帳號已存在；沒有新增。")
        if len(memos) >= 200:
            raise ValueError("備忘錄最多 200 筆；沒有新增。")
        memos.append({"system": system, "account": account, "password": password})
    elif operation in ("update", "delete"):
        original_system = fields.get("originalSystem", "")
        original_account = fields.get("originalAccount", "")
        matches = [index for index, item in enumerate(memos)
                   if item["system"] == original_system
                   and item["account"] == original_account]
        if len(matches) != 1:
            raise ValueError("找不到指定的系統與帳號；沒有修改。")
        index = matches[0]
        if operation == "delete":
            memos.pop(index)
        else:
            system = validate_memo_label(fields.get("system", ""), "系統")
            account = validate_memo_label(fields.get("account", ""), "帳號")
            if any(item["system"] == system and item["account"] == account
                   for i, item in enumerate(memos) if i != index):
                raise ValueError("新的系統與帳號已存在；沒有修改。")
            password = fields.get("password", "")
            memos[index] = {"system": system, "account": account,
                            "password": validate_memo_password(password) if password
                            else memos[index]["password"]}
    else:
        raise ValueError("不支援的備忘錄操作。")
    save_json(PASSWORD_JSON, memos, private=True)


def portal_cookie_from_parts(parts: dict[str, str]) -> str:
    cookies = []
    for name in SESSION_COOKIE_NAMES:
        value = parts.get(name, "").strip()
        if value.startswith(name + "="):
            value = value[len(name) + 1:]
        if not value or any(char in value for char in ";\r\n"):
            raise ValueError("請分別填入 portal、SESSION、XSRF-TOKEN 的有效值。")
        cookies.append((name, value))
    header = "; ".join(f"{name}={value}" for name, value in cookies)
    if parse_cookie_header(header) != cookies:
        raise ValueError("Session Cookie 格式不正確。")
    return header


def portal_cookie_from_header(raw: str) -> str:
    if any(char in raw for char in "\r\n"):
        raise ValueError("Session Cookie 格式不正確。")
    try:
        cookies = parse_cookie_header(raw)
    except ValueError as exc:
        raise ValueError("Portal Session 無法辨識，請重新貼上 Cookie。") from exc
    cookie_values = dict(cookies)
    if any(not cookie_values.get(name) for name in SESSION_COOKIE_NAMES):
        raise ValueError("Session 需要包含 portal、SESSION、XSRF-TOKEN。")
    return "; ".join(f"{name}={value}" for name, value in cookies)


def saved_portal_cookie() -> str:
    try:
        value = json.loads(SESSION_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("session.json 無法讀取，請修正後再連線。") from exc
    if not isinstance(value, dict):
        raise ValueError("session.json 格式不正確。")
    if isinstance(value.get("portalCookie"), str):
        return value["portalCookie"]  # 相容舊版已保存的 Cookie header。
    if all(not value.get(name) for name in SESSION_COOKIE_NAMES):
        return ""
    if not all(isinstance(value.get(name), str) for name in SESSION_COOKIE_NAMES):
        raise ValueError("session.json 需要 portal、SESSION、XSRF-TOKEN 三個欄位。")
    return portal_cookie_from_parts(value)


def automatic_portal_cookie() -> str:
    """Run the existing browser login without changing the saved Session yet."""
    with tempfile.TemporaryDirectory(prefix="ncu-portal-login-") as directory:
        temporary_session = Path(directory) / "session.json"
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("portal_login.py")),
                 "--session", str(temporary_session)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=PORTAL_LOGIN_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Portal 自動登入逾時；原 Session 未變更。") from exc
        except OSError as exc:
            raise ValueError("無法啟動 Portal 自動登入；原 Session 未變更。") from exc
        if result.returncode != 0:
            raise ValueError("Portal 自動登入未成功，請檢查 Chrome／Buster；原 Session 未變更。")
        try:
            stored = json.loads(temporary_session.read_text(encoding="utf-8"))
            cookie = stored["portalCookie"]
            if not isinstance(cookie, str):
                raise TypeError
            return portal_cookie_from_header(cookie)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("Portal 自動登入沒有取得有效 Session；原 Session 未變更。") from exc


def saved_account_cookies() -> dict[str, str]:
    try:
        stored = json.loads(ACCOUNT_SESSION_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("session01.json 無法讀取，請修正後再連線。") from exc
    if not isinstance(stored, dict) or not isinstance(stored.get("accountCookie"), str):
        raise ValueError("session01.json 格式不正確。")
    try:
        return account_cookies_from_header(stored["accountCookie"])
    except ValueError as exc:
        raise ValueError("session01.json 內的報帳 Cookie 格式不正確。") from exc


def automatic_account_cookies() -> dict[str, str]:
    """Log in using the captcha visible in one isolated browser tab."""
    with tempfile.TemporaryDirectory(prefix="ncu-account-login-") as directory:
        temporary_session = Path(directory) / "session01.json"
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("account_login.py")),
                 "--session", str(temporary_session)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=ACCOUNT_LOGIN_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("報帳自動登入逾時；原 Session 未變更。") from exc
        except OSError as exc:
            raise ValueError("無法啟動報帳自動登入；原 Session 未變更。") from exc
        if result.returncode != 0:
            output = result.stdout if isinstance(result.stdout, bytes) else b""
            if "驗證碼".encode("utf-8") in output:
                raise ValueError("主計室驗證碼無法可靠辨識；原 Session 未變更。")
            if "MAIN 頁驗證".encode("utf-8") in output:
                raise ValueError("主計室登入後未取得可讀取的 MAIN 頁；原 Session 未變更。")
            raise ValueError("報帳自動登入未成功，請檢查帳密或驗證碼辨識；原 Session 未變更。")
        try:
            stored = json.loads(temporary_session.read_text(encoding="utf-8"))
            header = stored["accountCookie"]
            if not isinstance(header, str):
                raise TypeError
            return account_cookies_from_header(header)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("報帳自動登入沒有取得有效 Session；原 Session 未變更。") from exc


def verify_account_cookies(values: dict[str, str]) -> None:
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    anonymous = probe_account_session({}, context, MAIN_FRAME_TARGET)
    result = probe_account_session(values, context, MAIN_FRAME_TARGET)
    expected_path = urllib.parse.urlsplit(MAIN_FRAME_TARGET).path
    if (result.status != 200 or result.path != expected_path
            or result.password_field or result.offsite_host
            or (anonymous.status == result.status
                and anonymous.body_digest == result.body_digest)):
        same_body = (anonymous.status == result.status
                     and anonymous.body_digest == result.body_digest)
        raise ValueError(
            "這組 Cookie 尚未證明可讀取報帳 MAIN 頁面；沒有保存 Session。"
            f"唯讀診斷：帶 Cookie HTTP {result.status or '無回應'}，"
            f"未帶 Cookie HTTP {anonymous.status or '無回應'}，"
            f"最終路徑 {result.path or '無'}，"
            f"內容與未帶 Cookie {'相同' if same_body else '不同'}，"
            f"登入欄位 {'有' if result.password_field else '無'}，"
            f"跨站跳轉 {'有' if result.offsite_host else '無'}。"
        )


def account_cookie_header(values: dict[str, str]) -> str:
    header = "; ".join(f"{name}={value}" for name, value in values.items())
    if account_cookies_from_header(header) != values:
        raise ValueError("報帳 Cookie 格式不正確。")
    return header


def blank_record() -> dict[str, object]:
    return record_from_form({}, [""])


def bootstrap(portal_cookie: str) -> LocalSession:
    cookies = parse_cookie_header(portal_cookie)
    jar = http.cookiejar.CookieJar()
    seed_cookie_jar(jar, "portal.ncu.edu.tw", cookies)
    cookies.clear()
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    opener = urllib.request.build_opener(
        CookieTracker(jar), urllib.request.HTTPSHandler(context=context), RedirectTracker(20)
    )
    response = open_page(opener, DEFAULT_TARGET, DEFAULT_USER_AGENT)
    consent_submitted = False
    for _ in range(5):
        body = response.read(2_000_000)
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        bridge = find_sso_form(body, content_type, final_url)
        if bridge:
            method, action, fields = bridge
            target = urllib.parse.urlsplit(action)
            if target.scheme != "https" or target.hostname not in (
                "portal.ncu.edu.tw", "cis.ncu.edu.tw"
            ):
                raise ValueError("登入導向了非預期網站，已停止。")
            encoded = urllib.parse.urlencode(fields).encode("utf-8")
            response = open_page(opener, action, DEFAULT_USER_AGENT,
                                 data=encoded if method == "post" else None,
                                 referer=final_url)
            continue
        consent = (find_consent_submission(body, content_type, final_url)
                   if not consent_submitted else None)
        if consent:
            action, fields = consent
            response = open_page(opener, action, DEFAULT_USER_AGENT,
                                 data=urllib.parse.urlencode(fields).encode("utf-8"),
                                 referer=final_url)
            consent_submitted = True
            continue
        break
    response = open_page(opener, DEFAULT_TARGET, DEFAULT_USER_AGENT)
    body = response.read(2_000_000)
    if (response.getcode() != 200 or response.geturl() != DEFAULT_TARGET
            or b"businessApply" not in body or b'name="_token"' not in body):
        raise ValueError("尚未取得 HumanSys 公差新增頁；請確認 Portal Cookie 仍有效。")
    return LocalSession(opener, secrets.token_urlsafe(32), time.monotonic() + SESSION_TTL)


def fetch_list(session: LocalSession):
    response = open_page(session.opener, LIST_URL, DEFAULT_USER_AGENT)
    body = response.read(2_000_000)
    rows = parse_list(body, response.headers.get("Content-Type", ""), response.geturl())
    return rows, body


def record_from_form(form: dict[str, str], filenames: list[str]) -> dict[str, object]:
    return {
        "startDate": form.get("startDate", "").strip(),
        "endDate": form.get("endDate", "").strip(),
        "startTime": form.get("startTime", "").strip(),
        "endTime": form.get("endTime", "").strip(),
        "destinationDistrict": form.get("destinationDistrict", "").strip(),
        "includeHoliday": form.get("includeHoliday", "").strip(),
        "location": form.get("location", "").strip(),
        "reason": form.get("reason", "").strip(),
        "proofFile": [{"filename": name} for name in filenames],
    }


def page(title: str, content: str, active: str = "business") -> bytes:
    business_current = ' aria-current="page"' if active == "business" else ""
    account_current = ' aria-current="page"' if active == "account" else ""
    personnel_current = ' aria-current="page"' if active == "personnel" else ""
    research_current = ' aria-current="page"' if active == "research" else ""
    appropriation_current = ' aria-current="page"' if active == "appropriation" else ""
    memo_current = ' aria-current="page"' if active == "memo" else ""
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · 表單工作台</title>
<style>
:root{{font-family:system-ui,-apple-system,'Noto Sans TC',sans-serif;color:#17233b;background:#f5f7fb}}
*{{box-sizing:border-box}} body{{margin:0}} main{{max-width:1050px;margin:32px auto;padding:0 18px}}
header{{background:#142f59;color:white;padding:18px 22px}} header h1{{margin:0;font-size:22px}}
.topbar{{max-width:1050px;margin:auto}}.tabs{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}}
.tabs a{{color:#dbe9ff;text-decoration:none;padding:9px 15px;border-radius:8px;border:1px solid #6d8ab4}}
.tabs a[aria-current=page]{{background:white;color:#142f59;border-color:white;font-weight:700}}
.card{{background:#fff;border:1px solid #dce3ee;border-radius:12px;padding:22px;margin:18px 0;box-shadow:0 3px 18px #142f5910}}
h2{{margin:0 0 16px;font-size:20px}} p{{line-height:1.55}} .note{{color:#52627b;font-size:14px}}
.warn{{background:#fff5e8;border-left:4px solid #db892b;padding:12px 15px;border-radius:6px}}
.ok{{background:#eaf8ef;border-left:4px solid #28965a;padding:12px 15px;border-radius:6px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
.memo-system{{grid-column:1/-1}}
label{{display:block;font-weight:600;margin:12px 0 5px}} input,textarea,select{{font:inherit;width:100%;padding:10px;border:1px solid #aebbd0;border-radius:7px;background:white}}
textarea{{min-height:90px}} input[type=checkbox],input[type=radio]{{width:auto;margin-right:7px}}
button,.button{{display:inline-block;border:0;border-radius:7px;background:#2156a0;color:white;padding:10px 16px;font:inherit;cursor:pointer;text-decoration:none;margin:8px 8px 0 0}}
button.secondary,.button.secondary{{background:#e8eef7;color:#173967}}button.danger{{background:#a53d37}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border-bottom:1px solid #dce3ee;padding:8px;text-align:left;vertical-align:top}}th{{background:#f1f5fa}}
.scroll{{overflow:auto}}.inline{{display:inline}}.inline input{{width:auto}}.check{{font-weight:400}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><header><div class="topbar"><h1>表單工作台</h1><small>{esc(APP_VERSION)}</small>
<nav class="tabs" aria-label="功能頁面"><a href="/"{business_current}>公差申請 (人事系統)</a>
<a href="/account"{account_current}>公差報帳 (主計室)</a>
<a href="/research"{research_current}>計劃檢視 (研究發展系統)</a>
<a href="/appropriation"{appropriation_current}>主持人費撥帳 (撥帳系統)</a>
<a href="/account/personnel"{personnel_current}>主持人費報帳 (主計室)</a>
<a href="/memo"{memo_current}>備忘錄</a></nav></div></header><main>{content}</main></body></html>""".encode("utf-8")


def dashboard(session: LocalSession | None, message: str = "", load_input: bool = False) -> bytes:
    values = load_defaults() if load_input else blank_record()
    filename_list = default_filenames(values)
    filename = filename_list[0] if filename_list else ""
    note = f'<p class="ok">{esc(message)}</p>' if message and message != "Session 已保存。" else ""
    csrf = session.csrf if session else START_CSRF
    selected = str(values.get("includeHoliday", ""))
    holiday_select = (f'<option value="" {"selected" if not selected else ""}>請選擇</option>'
                      f'<option value="N" {"selected" if selected == "N" else ""}>否</option>'
                      f'<option value="Y" {"selected" if selected == "Y" else ""}>是</option>')
    return page("操作", f"""{note}<section class="card"><h2>1. Session</h2>
<form method="post" action="/connect" autocomplete="off"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<label for="portalCookie"></label><p class="note">主要包含 ① portal ② SESSION ③ XSRF-TOKEN。</p>
<input id="portalCookie" name="portalCookie" type="password" autocomplete="off" spellcheck="false">
<button type="submit">手動更新</button></form>
<form method="post" action="/auto-connect" class="inline"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<button type="submit">自動更新</button></form>{session_saved_status(session is not None)}</section>
<section class="card"><h2>2. 填寫公差申請</h2>
<a class="button secondary" href="/?load=input">預設資料 (input.json)</a>
<form method="post" action="/draft" enctype="multipart/form-data" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<div class="grid"><div><label>startDate (民國115年1月1號，填1150101)</label><input name="startDate" required value="{esc(values.get('startDate',''))}"></div>
<div><label>endDate</label><input name="endDate" required value="{esc(values.get('endDate',''))}"></div>
<div><label>startTime</label><input name="startTime" type="text" maxlength="5" pattern="(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]" placeholder="例如 17:00" required value="{esc(values.get('startTime',''))}"></div>
<div><label>endTime</label><input name="endTime" type="text" maxlength="5" pattern="(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]" placeholder="例如 17:00" required value="{esc(values.get('endTime',''))}"></div>
<div><label>destinationDistrict（407 臺中市 西屯區、744 臺南市 新市區）</label><input name="destinationDistrict" required value="{esc(values.get('destinationDistrict',''))}"></div>
<div><label>includeHoliday（是否含假日時段）</label><select name="includeHoliday" required>{holiday_select}</select></div></div>
<label>location（地點／機關）</label><input name="location" required value="{esc(values.get('location',''))}">
<label>reason（最多 100 字）</label><textarea name="reason" maxlength="100" required>{esc(values.get('reason',''))}</textarea>
<label>proofFile[0].filename（本機證明檔案）</label><input name="proofFilename" required value="{esc(filename)}">
<p><label class="check"><input type="checkbox" name="confirmDraft" value="yes">我已核對資料</label></p>
<button type="submit" name="operation" value="save">儲存 (不送審)</button>
</form></section>
<section class="card"><h2>3. 查詢並送審既有申請</h2>
<form method="post" action="/list"><input type="hidden" name="csrf" value="{esc(csrf)}">
<input name="date" aria-label="篩選起日" value="" placeholder="篩選起日，例如 1151027；留空查全部">
<button type="submit">查詢申請清單</button></form></section>
<section class="card"><h2>4. 列印申請單</h2>
<form method="post" action="/business-record/list"><input type="hidden" name="csrf" value="{esc(csrf)}">
<button type="submit">列出公差紀錄</button></form></section>
{f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="secondary" type="submit">結束本機工作階段</button></form>' if session else ''}""")


ACCOUNT_FIELDS = (
    ("abstract", "用途說明／摘要"), ("budgetNo", "計畫／經費代碼"),
    ("budgetAmount", "經費金額"), ("product", "品名"),
    ("productAmount", "品名金額"), ("payeeAmount", "受款人金額"),
    ("invoiceNo", "發票號碼"), ("invoiceDate", "發票／憑證日期"),
)

PERSONNEL_DEFAULTS = {
    "abstract": "範例主持人費，請改為實際用途。",
    "budgetNo": "11421012",
    "amount": "25000",
    "payeeAmount": "25000",
    "payeeList": "yes",
}


def personnel_record_from_form(form: dict[str, str]) -> dict[str, str]:
    return {key: form.get(key, "").strip() for key in PERSONNEL_DEFAULTS}


def load_personnel_defaults() -> dict[str, str]:
    try:
        data = json.loads(PERSONNEL_INPUT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 input03.json：{exc}") from exc
    if (not isinstance(data, dict) or
            any(not isinstance(data.get(key), str)
                for key in ("abstract", "budgetNo", "amount", "payeeAmount")) or
            not isinstance(data.get("payeeList"), bool)):
        raise ValueError("input03.json 的主持人費欄位格式不正確。")
    record = {key: data[key].strip() for key in ("abstract", "budgetNo", "amount", "payeeAmount")}
    record["payeeList"] = "yes" if data["payeeList"] else "no"
    validate_personnel_record(record)
    return record


def validate_personnel_record(record: dict[str, str]) -> None:
    if not record["abstract"] or len(record["abstract"]) > 1000:
        raise ValueError("請填用途說明（最多 1000 字）。")
    if any(char in record["abstract"] for char in "#@◎\r\n"):
        raise ValueError("用途說明含官方明細分隔字元。")
    if not re.fullmatch(r"[A-Za-z0-9]{1,20}", record["budgetNo"]):
        raise ValueError("請填有效的計畫／經費代碼。")
    if not re.fullmatch(r"[0-9]{1,10}", record["amount"]) or int(record["amount"]) <= 0:
        raise ValueError("金額要是正整數，最多 10 位。")
    if not re.fullmatch(r"[0-9]{1,10}", record["payeeAmount"]) or int(record["payeeAmount"]) <= 0:
        raise ValueError("受款人金額要是正整數，最多 10 位。")
    if record["payeeAmount"] != record["amount"]:
        raise ValueError("經費金額與受款人金額必須相同。")
    if record["payeeList"] != "yes":
        raise ValueError("請勾選詳如清冊。")


def personnel_prefill_script(record: dict[str, str]) -> str:
    """Fill the official personnel form using its own ATOLIS handler; never save."""
    validate_personnel_record(record)
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    script = r'''(async () => {
  "use strict";
  const data = __PERSONNEL_DATA__;
  const fail = message => { throw new Error(message + "；沒有按『存入』。"); };
  if (location.hostname !== "account.ncu.edu.tw") fail("請在官方報帳網站執行");
  let root = window;
  try { if (window.top.document) root = window.top; } catch (_) {}
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const collect = () => {
    const forms = [], inputs = [], seen = new Set();
    const visit = view => {
      if (seen.has(view) || seen.size > 64) return;
      seen.add(view);
      try {
        if (view.location.hostname !== "account.ncu.edu.tw") return;
        forms.push(...view.document.forms);
        inputs.push(...view.document.querySelectorAll("input"));
        Array.from(view.frames).forEach(visit);
      } catch (_) {}
    };
    visit(root);
    return {forms, inputs};
  };
  const unique = (items, predicate, label) => {
    const matches = items.filter(predicate);
    if (matches.length > 1) fail(label + "找到多個");
    return matches[0] || null;
  };
  const control = (form, name) => {
    const value = form?.elements.namedItem(name);
    if (!value || !("value" in value)) fail("找不到官方欄位 " + name);
    return value;
  };
  const locate = () => {
    const {forms, inputs} = collect();
    const main = unique(forms, f => ["ABSTRACT", "BUGETNO_1", "BUGCODE_1",
      "D_AMOUNT_1", "APPYSET", "MOVETYPE_1"].every(n => f.elements.namedItem(n)), "經費表單");
    const payee = unique(forms, f => ["VENNAME_1", "VENDORID_1", "AMOUNT_1"].every(
      n => f.elements.namedItem(n)), "受款人表單");
    const save = unique(forms, f => {
      const u = new URL(f.action, f.ownerDocument.location.href);
      return u.hostname === "account.ncu.edu.tw" &&
        u.pathname === "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp" &&
        f.method.toLowerCase() === "post";
    }, "儲存表單");
    return {main, payee, save, inputs};
  };
  const waitFor = async (find, label) => {
    for (let n = 0; n < 60; n++) {
      const result = find();
      if (result) return result;
      await delay(250);
    }
    fail("找不到" + label + "，請檢查官方頁面");
  };
  const ready = () => {
    const found = locate();
    return found.main && found.payee && found.save &&
      found.main.ownerDocument.readyState === "complete" ? found : null;
  };
  let found = ready();
  if (!found && locate().main) found = await waitFor(ready, "編輯頁載入");
  if (!found) {
    const button = (value, onclick) => unique(collect().inputs,
      x => x.type === "button" && x.value.trim() === value &&
        (x.getAttribute("onclick") || "").includes(onclick), value);
    let choice = unique(collect().inputs, x => x.type === "checkbox" &&
      x.name === "CHK5" && x.closest("tr")?.textContent.includes("人事費用報支"), "人事費用報支");
    if (!choice) {
      let add = button("新增請購", "aBT2.click");
      if (!add) {
        const query = button("計畫請購查詢", "LIS4");
        if (!query) fail("找不到計畫請購查詢或新增請購");
        query.click();
        add = await waitFor(() => button("新增請購", "aBT2.click"), "新增請購");
      }
      add.click();
      choice = await waitFor(() => unique(collect().inputs, x => x.type === "checkbox" &&
        x.name === "CHK5" && x.closest("tr")?.textContent.includes("人事費用報支"),
        "人事費用報支"), "人事費用報支選項");
    }
    if (!choice.checked) choice.click(); // 觸發官方 CK5，不直接改 hidden 欄位。
    const category = choice.form;
    if (!category || !choice.checked || control(category, "APPYSET").value !== "15" ||
        control(category, "MOVETYPE").value !== "6" ||
        control(category, "SETNAME").value.trim() !== "人事費用報支" ||
        control(category, "ISCASH").value !== "False" ||
        control(category, "SHOWPRO").value !== "False") {
      fail("官方人事費用報支選項設定不符");
    }
    const next = control(category, "SSS");
    const target = new URL(category.action, category.ownerDocument.location.href);
    if (next.type !== "submit" || next.value.trim() !== "下一步" ||
        target.hostname !== "account.ncu.edu.tw" ||
        target.pathname === "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp") {
      fail("官方下一步表單設定不符");
    }
    next.click();
    found = await waitFor(ready, "人事費用報支表單");
  }
  let {main, payee} = found;
  const mode = () => {
    if (control(main, "APPYSET").value !== "15" ||
        control(main, "MOVETYPE_1").value !== "6" ||
        control(main, "NO_MATH").value !== "N") fail("目前不是人事費用報支表單");
  };
  const choose = (name, value, eventName) => {
    const el = control(main, name);
    if (el.tagName !== "SELECT" || !Array.from(el.options).some(o => o.value === value))
      fail("官方下拉選單沒有 " + name + "=" + value);
    el.value = value;
    el.dispatchEvent(new el.ownerDocument.defaultView.Event(eventName, {bubbles:true}));
  };
  mode();
  choose("BUGETNO_1", data.budgetNo, "change");
  found = await waitFor(() => {
    const current = ready();
    const options = current?.main.elements.namedItem("BUGCODE_1")?.options;
    return current && current.main.elements.namedItem("BUGETNO_1")?.value === data.budgetNo &&
      options && Array.from(options).some(o => o.value === "300") ? current : null;
  }, "選擇計畫後的業務費選項");
  ({main, payee} = found);
  mode();
  if (control(main, "BUGETNO_1").value !== data.budgetNo) fail("計畫選擇未保留");
  choose("BUGCODE_1", "300", "blur");
  await delay(700);
  found = await waitFor(ready, "選擇業務費後的表單");
  ({main, payee} = found);
  mode();
  if (control(main, "BUGETNO_1").value !== data.budgetNo ||
      control(main, "BUGCODE_1").value !== "300") fail("計畫或業務費未保留");
  const set = (form, name, value) => {
    const el = control(form, name);
    el.value = value;
    el.dispatchEvent(new el.ownerDocument.defaultView.Event("input", {bubbles:true}));
    el.dispatchEvent(new el.ownerDocument.defaultView.Event("blur", {bubbles:true}));
  };
  set(main, "ABSTRACT", data.abstract);
  set(main, "D_AMOUNT_1", data.amount);
  const button = unique(collect().inputs, x => x.type === "button" &&
    x.value.trim() === "詳如清冊" && (x.getAttribute("onclick") || "").includes("ATOLIS"),
    "詳如清冊按鈕");
  if (!button || button.ownerDocument !== payee.ownerDocument)
    fail("找不到受款人頁的詳如清冊按鈕");
  button.click(); // 使用官方 ATOLIS() 回填受款人，不推測識別資料。
  found = await waitFor(() => {
    const current = ready();
    return current && current.payee.elements.namedItem("VENNAME_1")?.value &&
      current.payee.elements.namedItem("VENDORID_1")?.value ? current : null;
  }, "詳如清冊受款人資料");
  ({main, payee} = found);
  set(payee, "AMOUNT_1", data.payeeAmount);
  if (!control(payee, "VENNAME_1").value || !control(payee, "VENDORID_1").value)
    fail("官方詳如清冊按鈕未完整回填受款人欄位");
  console.info("人事費用報支欄位已填入。請在官方頁核對後親自按『存入』。本腳本沒有存入。");
  return "filled";
})()'''
    return script.replace("__PERSONNEL_DATA__", data)


def personnel_page(message: str = "", values: dict[str, str] | None = None,
                   script: str = "", connected: bool = False) -> bytes:
    record = values if values is not None else {}
    notice = f'<p class="ok">{esc(message)}</p>' if message else ""
    result = (f'<section class="card"><h2>填寫腳本</h2><textarea readonly '
              f'aria-label="人事費填寫腳本" spellcheck="false" '
              f'style="min-height:340px;font-family:monospace;font-size:13px">'
              f'{esc(script)}</textarea></section>') if script else ""
    return page("主持人費報帳", f'''<section class="card"><h2>1. 報帳 Cookie</h2>
<p class="note">貼上目前瀏覽器中的所有 ASPSESSIONID Cookie。</p>
<form method="post" action="/account/connect" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="returnTo" value="/account/personnel">
<input id="accountCookie" name="accountCookie" type="password" aria-label="報帳 Cookie" autocomplete="off" spellcheck="false">
<button type="submit">手動更新</button></form>
<form method="post" action="/account/auto-connect" class="inline"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="returnTo" value="/account/personnel"><button id="accountLoginAutoUpdate" type="submit">自動更新</button></form>
{session_saved_status(connected)}</section>
<section class="card"><h2>2. 填寫報帳資料</h2>
<a class="button secondary" href="/account/personnel?load=input03">預設資料 (input03.json)</a>
{notice}<form method="post" action="/account/personnel/save" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<label for="personnelAbstract">用途說明</label>
<textarea id="personnelAbstract" name="abstract" maxlength="1000" required>{esc(record.get('abstract', ''))}</textarea>
<div class="grid"><div><label for="personnelBudget">計畫／經費代碼</label>
<input id="personnelBudget" name="budgetNo" required value="{esc(record.get('budgetNo', ''))}"></div>
<div><label>經費用途</label><select disabled><option>300 業務費</option></select></div>
<div><label for="personnelAmount">經費金額</label>
<input id="personnelAmount" name="amount" inputmode="numeric" required value="{esc(record.get('amount', ''))}"></div>
<div><label for="personnelPayeeAmount">受款人金額</label>
<input id="personnelPayeeAmount" name="payeeAmount" inputmode="numeric" required value="{esc(record.get('payeeAmount', ''))}"></div></div>
<label><input type="checkbox" name="payeeList" value="yes"{' checked' if record.get('payeeList') == 'yes' else ''}> 詳如清冊</label>
<button type="submit">存入</button>
<button type="submit" formaction="/account/personnel/prefill" class="secondary">產生填寫腳本（不存入）</button></form>
</section>{account_print_form('15')}{result}''', active="personnel")


def account_record_from_form(form: dict[str, str]) -> dict[str, str]:
    record = {key: form.get(key, "").strip() for key, _ in ACCOUNT_FIELDS}
    record["budgetCode"] = "300"
    record["advancePaid"] = "yes" if form.get("advancePaid") == "yes" else "no"
    return record


def load_account_defaults() -> dict[str, str]:
    try:
        data = json.loads(ACCOUNT_INPUT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 input01.json：{exc}") from exc
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) for key, _ in ACCOUNT_FIELDS
    ) or data.get("budgetCode") != "300" or not isinstance(data.get("advancePaid"), bool):
        raise ValueError("input01.json 的報帳欄位格式不正確。")
    record = {key: data[key].strip() for key, _ in ACCOUNT_FIELDS}
    record["budgetCode"] = "300"
    record["advancePaid"] = "yes" if data["advancePaid"] else "no"
    validate_account_record(record)
    return record


def validate_account_record(record: dict[str, str]) -> None:
    if not record["abstract"] or len(record["abstract"]) > 1000:
        raise ValueError("請填摘要（最多 1000 字）。")
    if not re.fullmatch(r"[A-Za-z0-9]{1,20}", record["budgetNo"]):
        raise ValueError("請填有效的計畫／經費代碼。")
    if not record["product"] or len(record["product"]) > 150:
        raise ValueError("請填品名（最多 150 字）。")
    amounts = []
    for name in ("budgetAmount", "productAmount", "payeeAmount"):
        value = record[name]
        if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) <= 0:
            raise ValueError("經費、品名與受款人金額都要是正整數，最多 10 位。")
        amounts.append(int(value))
    if len(set(amounts)) != 1:
        raise ValueError("經費、品名與受款人金額必須相同。")
    if record["invoiceNo"] and len(record["invoiceNo"]) > 10:
        raise ValueError("發票號碼最多 10 字。")
    if record["invoiceDate"]:
        raw = record["invoiceDate"]
        if not re.fullmatch(r"\d{6,7}", raw):
            raise ValueError("憑證日期請填民國 6 或 7 位數字。")
        try:
            date(int(raw[:-4]) + 1911, int(raw[-4:-2]), int(raw[-2:]))
        except ValueError as exc:
            raise ValueError("憑證日期不是有效日期。") from exc


def account_prefill_script(record: dict[str, str]) -> str:
    """Return a console helper that never invokes final save or reads cookies."""
    validate_account_record(record)
    if record["advancePaid"] != "yes":
        raise ValueError("目前只支援勾選代墊，由報帳網站帶入受款人。")
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    script = r"""(async () => {
  "use strict";
  const data = __ACCOUNT_DATA__;
  const fail = message => { throw new Error(message + "；沒有按「存入」。"); };
  if (location.hostname !== "account.ncu.edu.tw") fail("請在官方報帳網站執行");
  let root = window;
  try { if (window.top.document) root = window.top; } catch (error) { /* 只搜尋同源頁框 */ }
  const scan = (view, forms, documents, seen) => {
    if (seen.has(view)) return;
    seen.add(view);
    if (seen.size > 64) fail("頁框數量超過預期，請重新載入官方頁面");
    let children;
    try {
      if (view.location.hostname !== "account.ncu.edu.tw") return;
      forms.push(...view.document.forms);
      documents.push(view.document);
      children = Array.from(view.frames);
    } catch (error) { return; /* 不跨網域讀取頁框 */ }
    children.forEach(child => scan(child, forms, documents, seen));
  };
  const collect = () => {
    const forms = [], documents = [];
    scan(root, forms, documents, new Set());
    return {forms, documents};
  };
  const has = (form, names) => names.every(name => form.elements.namedItem(name));
  const locate = () => {
    const {forms} = collect();
    const unique = (label, predicate) => {
      const matches = forms.filter(predicate);
      if (matches.length > 1) fail(label + "表單找到多份，已停止");
      return matches[0] || null;
    };
    return {
      main: unique("經費", form => has(form, [
        "ABSTRACT", "BUGETNO_1", "BUGCODE_1", "D_AMOUNT_1", "MOVETYPE_1", "NO_MATH", "APPYBUY"
      ])),
      item: unique("品名", form => has(form, ["PRODUCT_1", "AMOUNT_1"])),
      payee: unique("受款人", form => has(form, [
        "PROX_1", "INVOICENO_1", "IDATE_1", "AMOUNT_1", "VENNAME_1"
      ])),
      save: unique("儲存", form => {
        const url = new URL(form.action, form.ownerDocument.location.href);
        return url.hostname === "account.ncu.edu.tw" &&
          url.pathname === "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp" &&
          form.method.toLowerCase() === "post";
      })
    };
  };
  const complete = found => found.main && found.item && found.payee && found.save;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const findInput = (label, predicate) => {
    const {documents} = collect();
    const matches = documents.flatMap(doc => Array.from(doc.querySelectorAll("input"))).filter(predicate);
    if (matches.length > 1) fail(label + "找到多個，已停止");
    return matches[0] || null;
  };
  const waitFor = async (get, label) => {
    for (let i = 0; i < 60; i++) {
      const result = get();
      if (result) return result;
      await delay(250);
    }
    fail("找不到" + label + "，請確認官方頁面已完成載入");
  };
  const queryButton = () => findInput("計畫請購查詢", input =>
    input.type === "button" && input.value.trim() === "計畫請購查詢" &&
    (input.getAttribute("onclick") || "").includes("LIS4"));
  const addButton = () => findInput("新增請購", input =>
    input.type === "button" && input.id === "BUTA2" && input.value.trim() === "新增請購");
  const categoryCheckbox = () => findInput("零用金先墊付報支", input =>
    input.type === "checkbox" && input.name === "CHK4" &&
    input.closest("tr")?.querySelector("font.size3DB")?.textContent.trim() ===
      "零用金先墊付報支（含國內旅費）");
  const waitFrames = async label => waitFor(() => {
    const current = locate();
    return complete(current) && current.main.ownerDocument.readyState === "complete" ? current : null;
  }, label + "後的報帳表單");
  let found = locate();
  if (found.main && !complete(found)) found = await waitFrames("編輯頁載入");
  if (!complete(found)) {
    let category = categoryCheckbox();
    if (!category) {
      let add = addButton();
      if (!add) {
        const query = queryButton();
        if (!query) fail("找不到計畫請購查詢、 新增請購或正確報支表單");
        query.click();
        add = await waitFor(addButton, "新增請購");
      }
      add.click();
      category = await waitFor(categoryCheckbox,
        "零用金先墊付報支選項（新增請購可能已開啟；請先檢查官方頁面，勿直接重試）");
    }
    const categoryForm = category.form;
    if (!categoryForm) fail("零用金選項不在官方表單內");
    if (!category.checked) category.click(); // 觸發官方 CK4，不能只改 checked。
    const categoryField = name => categoryForm.elements.namedItem(name)?.value;
    if (!category.checked || categoryField("APPYSET") !== "14" ||
        categoryField("MOVETYPE") !== "6" || categoryField("ISCASH") !== "True" ||
        categoryField("SETNAME") !== "零用金先墊付報支（含國內旅費）") {
      fail("零用金類別與官方頁面設定不符，沒有按下一步");
    }
    const next = categoryForm.elements.namedItem("SSS");
    const nextUrl = new URL(categoryForm.action, categoryForm.ownerDocument.location.href);
    if (!next || next.type !== "submit" || next.value.trim() !== "下一步" ||
        nextUrl.hostname !== "account.ncu.edu.tw" ||
        !nextUrl.pathname.startsWith("/APSWIS_Q/") ||
        nextUrl.pathname === "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp") {
      fail("下一步按鈕或目標頁不是已核對的報帳流程");
    }
    next.click();
    found = await waitFrames("下一步（可能已進入新請購；請檢查官方頁面，勿直接重試）");
  }
  let {main, item, payee} = found;
  const field = (form, name) => {
    const element = form.elements.namedItem(name);
    if (!element || !("value" in element)) fail("找不到欄位 " + name);
    return element;
  };
  const checkMode = form => {
    if (field(form, "MOVETYPE_1").value !== "6" ||
        field(form, "NO_MATH").value !== "N" ||
        field(form, "APPYBUY").value !== "") {
      fail("目前頁面不是已核對的零用金先墊付報支表單");
    }
  };
  checkMode(main);
  const event = (element, type) => new element.ownerDocument.defaultView.Event(type, {bubbles: true});
  const select = (form, name, value, label, eventType) => {
    const element = field(form, name);
    if (element.tagName !== "SELECT" ||
        !Array.from(element.options).some(option => option.value === value && !option.disabled)) {
      fail(label + "的選項 " + value + " 不在官方下拉選單中");
    }
    if (element.value === value) return false;
    element.value = value;
    element.dispatchEvent(event(element, eventType));
    return true;
  };
  if (select(main, "BUGETNO_1", data.budgetNo, "計畫", "change")) {
    await delay(1000); // 官方頁的 BN_1 可能重載內層頁框。
    found = await waitFrames("選擇計畫");
    ({main, item, payee} = found);
  }
  if (field(main, "BUGETNO_1").value !== data.budgetNo) {
    fail("官方頁更新後沒有保留計畫 " + data.budgetNo);
  }
  checkMode(main);
  if (select(main, "BUGCODE_1", "300", "業務費", "blur")) {
    await delay(700); // 官方頁的 BC_1 可能更新欄位。
    found = await waitFrames("選擇業務費");
    ({main, item, payee} = found);
  }
  if (field(main, "BUGETNO_1").value !== data.budgetNo ||
      field(main, "BUGCODE_1").value !== "300") {
    fail("官方頁更新後計畫或業務費未保留，請檢查官方頁面");
  }
  checkMode(main);
  const targetFields = [
    [main, "ABSTRACT"], [main, "D_AMOUNT_1"],
    [item, "PRODUCT_1"], [item, "AMOUNT_1"],
    [payee, "PROX_1"], [payee, "INVOICENO_1"],
    [payee, "IDATE_1"], [payee, "AMOUNT_1"]
  ];
  targetFields.forEach(([form, name]) => field(form, name));
  const set = (form, name, value) => {
    const element = field(form, name);
    element.value = value;
    element.dispatchEvent(event(element, "input"));
    element.dispatchEvent(event(element, "blur"));
  };
  const prox = field(payee, "PROX_1");
  if (!prox.checked) prox.click();
  if (!prox.checked) fail("代墊沒有成功勾選，請手動確認");
  set(main, "ABSTRACT", data.abstract);
  set(main, "D_AMOUNT_1", data.budgetAmount);
  set(item, "PRODUCT_1", data.product);
  set(item, "AMOUNT_1", data.productAmount);
  set(payee, "INVOICENO_1", data.invoiceNo);
  set(payee, "IDATE_1", data.invoiceDate);
  set(payee, "AMOUNT_1", data.payeeAmount);
  const payeeId = payee.elements.namedItem("VENDORID_1")?.value ||
                  payee.elements.namedItem("VENDORID_S_1")?.value;
  const payeeName = payee.elements.namedItem("VENNAME_1")?.value;
  if (!payeeId || !payeeName) fail("受款人資料未自動帶入，請不要存入");
  console.info("欄位已填入。請逐欄核對經費、品名、受款人、金額及日期；確認後由你親自按官方頁面的「存入」。");
})()"""
    return script.replace("__ACCOUNT_DATA__", data)


def account_page(connected: bool = False, message: str = "",
                 values: dict[str, str] | None = None,
                 prefill_script: str = "") -> bytes:
    data = values or {}
    notice = f'<p class="ok">{esc(message)}</p>' if message else ""
    checked = ' checked' if data.get("advancePaid", "yes") == "yes" else ""
    summary = ""
    if prefill_script:
        summary += (f'<section class="card"><h2>填入官方頁面的腳本</h2>'
                    '<p>在官方報帳網站開啟選單頁即可；腳本會進入「計畫請購查詢」、按「新增請購」、'
                    '選「零用金先墊付報支（含國內旅費）」並按「下一步」，再選計畫及「300 業務費」。'
                    '在官方報帳頁開啟開發者工具 Console；點下方腳本框，按 ⌘A、⌘C，'
                    '貼到 Console 執行。它會操作選單和「下一步」，但不會按最後的「存入」。</p>'
                    f'<textarea readonly aria-label="報帳填寫腳本" spellcheck="false" '
                    f'style="min-height:340px;font-family:monospace;font-size:13px">{esc(prefill_script)}</textarea>'
                    '<p class="warn">執行後請在官方頁面逐欄核對，確認無誤後親自按「存入」。'
                    '腳本包含你輸入的報帳資料，請勿貼到公開聊天或與他人分享。</p></section>')
    return page("報帳", f"""{notice}<section class="card"><h2>1. 報帳 Cookie</h2>
<p class="note">貼上目前瀏覽器中的所有 ASPSESSIONID Cookie。</p>
<form method="post" action="/account/connect" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input id="accountCookie" name="accountCookie" type="password" aria-label="報帳 Cookie" autocomplete="off" spellcheck="false">
<button type="submit">手動更新</button></form>
<form method="post" action="/account/auto-connect" class="inline"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<button id="accountLoginAutoUpdate" type="submit">自動更新</button></form>
{session_saved_status(connected)}</section>
<section class="card"><h2>2. 填寫報帳資料</h2>
<a class="button secondary" href="/account?load=input01">預設資料 (input01.json)</a>
<form method="post" action="/account/save" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<label for="accountAbstract">用途說明／摘要</label><textarea id="accountAbstract" name="abstract" required maxlength="1000">{esc(data.get('abstract', ''))}</textarea>
<div class="grid"><div><label for="budgetNo">計畫／經費代碼</label>
<input id="budgetNo" name="budgetNo" required placeholder="例如 11516004" value="{esc(data.get('budgetNo', ''))}"></div>
<div><label for="budgetCode">經費用途</label><select id="budgetCode" disabled><option>300 業務費</option></select></div>
<div><label for="budgetAmount">經費金額</label><input id="budgetAmount" name="budgetAmount" inputmode="numeric" required value="{esc(data.get('budgetAmount', ''))}"></div>
<div><label for="product">品名</label><input id="product" name="product" required maxlength="150" value="{esc(data.get('product', ''))}"></div>
<div><label for="productAmount">品名金額</label><input id="productAmount" name="productAmount" inputmode="numeric" required value="{esc(data.get('productAmount', ''))}"></div>
<div><label for="payeeAmount">受款人金額</label><input id="payeeAmount" name="payeeAmount" inputmode="numeric" required value="{esc(data.get('payeeAmount', ''))}"></div>
<div><label for="invoiceNo">發票號碼（選填）</label><input id="invoiceNo" name="invoiceNo" maxlength="10" value="{esc(data.get('invoiceNo', ''))}"></div>
<div><label for="invoiceDate">發票／憑證日期（選填，民國 6～7 位數）</label><input id="invoiceDate" name="invoiceDate" inputmode="numeric" maxlength="7" value="{esc(data.get('invoiceDate', ''))}"></div></div>
<p><label class="check"><input type="checkbox" name="advancePaid" value="yes"{checked}>代墊（會自行帶入資料）</label></p>
<button type="submit">存入</button>
<button type="submit" formaction="/account/prefill" class="secondary">產生填寫腳本（不存入）</button>
</form></section>{account_print_form('14')}{summary}""", active="account")


def account_print_form(category: str) -> str:
    if category not in ("14", "15"):
        raise ValueError("不支援的報支類別。")
    return f'''<section class="card"><h2>3. 列印憑證單</h2>
<form method="post" action="/account/list">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="category" value="{category}">
<label for="accountYear">民國年份</label>
<input id="accountYear" name="year" inputmode="numeric" pattern="[0-9]{{2,3}}"
value="{date.today().year - 1911}" required>
<button type="submit">列出未審核案件</button></form></section>'''


def account_purchase_list_page(rows: list[PurchaseRow], year: int,
                               category: str = "14") -> bytes:
    if category not in ("14", "15"):
        raise ValueError("不支援的報支類別。")
    back = "/account/personnel" if category == "15" else "/account"
    active = "personnel" if category == "15" else "account"
    body = "".join(
        f"<tr><td>{esc(row.number)}</td><td>{esc(row.date)}</td>"
        f"<td>{esc(row.summary)}</td><td>{esc(row.amount)}</td>"
        f"<td>{esc(row.status)}</td><td>"
        f'<form method="post" action="/account/print-pdf" class="inline">'
        f'<input type="hidden" name="csrf" value="{esc(START_CSRF)}">'
        f'<input type="hidden" name="year" value="{esc(year)}">'
        f'<input type="hidden" name="category" value="{category}">'
        f'<input type="hidden" name="number" value="{esc(row.number)}">'
        f'<input type="hidden" name="addition" value="{esc(row.addition)}">'
        '<button type="submit">列印</button></form></td></tr>' for row in rows
    )
    if not body:
        body = '<tr><td colspan="6">目前清單沒有符合的案件。</td></tr>'
    return page("未審核案件", f"""<section class="card"><h2>民國 {year} 年未審核案件</h2>
<div class="scroll"><table><thead><tr><th>請購單號</th><th>日期</th><th>用途說明</th>
<th>金額</th><th>狀態</th><th>操作</th></tr></thead><tbody>{body}</tbody></table></div>
<a class="button secondary" href="{back}">返回報帳頁</a></section>""", active=active)


def research_portal_form(connected: bool = False) -> str:
    return f"""<section class="card"><h2>1. Session</h2>
<form method="post" action="/research/connect" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<label for="researchPortalCookie"></label>
<p class="note">主要包含 ① portal ② SESSION ③ XSRF-TOKEN。</p>
<input id="researchPortalCookie" name="portalCookie" type="password" autocomplete="off" spellcheck="false">
<button type="submit">手動更新</button></form>
<form method="post" action="/auto-connect" class="inline"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="returnTo" value="/research"><button type="submit">自動更新</button></form>{session_saved_status(connected)}</section>"""


def research_page(session: LocalSession | None = None, message: str = "",
                  success: bool = False) -> bytes:
    notice = (f'<p class="{"ok" if success else "warn"}">{esc(message)}</p>'
              if message and message != "Session 已保存。" else "")
    csrf = session.csrf if session else START_CSRF
    return page("計劃檢視", f"""{research_portal_form(session is not None)}
<section class="card"><h2>2. 列印經分表</h2>
{notice}<form method="post" action="/research/list">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<input type="hidden" name="isClose" value="">
<button type="submit">列出計劃</button></form>
</section>""",
                active="research")


def research_list_page(session: LocalSession, rows: list, is_close: str) -> bytes:
    body = []
    for row in rows:
        values = (row.number, row.period, row.name, row.principal, row.unit,
                  row.sponsor, row.start, row.end)
        cells = "".join(f"<td>{esc(value or '—')}</td>" for value in values)
        action = (f'<form method="post" action="/research/print" class="inline">'
                  f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
                  f'<input type="hidden" name="isClose" value="{esc(is_close)}">'
                  f'<input type="hidden" name="budgetId" value="{esc(row.budget_id)}">'
                  f'<input type="hidden" name="number" value="{esc(row.number)}">'
                  f'<input type="hidden" name="period" value="{esc(row.period)}">'
                  '<button type="submit">列印經分表</button></form>'
                  f'<form method="post" action="/research/human" class="inline">'
                  f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
                  f'<input type="hidden" name="isClose" value="{esc(is_close)}">'
                  f'<input type="hidden" name="budgetId" value="{esc(row.budget_id)}">'
                  f'<input type="hidden" name="number" value="{esc(row.number)}">'
                  f'<input type="hidden" name="period" value="{esc(row.period)}">'
                  '<button type="submit">列印甲式</button></form>'
                  f'<a class="button secondary" href="https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint?BudgetInfoId={esc(row.budget_id)}" '
                  'target="_blank" rel="noopener noreferrer">開啟官方頁</a>')
        body.append(f"<tr>{cells}<td>{action}</td></tr>")
    if not body:
        body.append('<tr><td colspan="9">目前清單沒有符合的計畫。</td></tr>')
    return page("計畫清單", f"""{research_portal_form(True)}
<section class="card"><h2>2. 列印經分表（{len(rows)} 筆）</h2>
<form method="post" action="/research/list">
<input type="hidden" name="csrf" value="{esc(session.csrf)}">
<label for="isClose">是否結案</label><select id="isClose" name="isClose">
<option value=""{' selected' if is_close == '' else ''}>全部</option>
<option value="N"{' selected' if is_close == 'N' else ''}>未結案</option>
<option value="Y"{' selected' if is_close == 'Y' else ''}>已結案</option></select>
<button type="submit">列出計劃</button></form>
<div class="scroll"><table><thead><tr><th>計畫流水號</th><th>期別</th><th>計畫名稱</th>
<th>計畫主持人</th><th>執行單位</th><th>委託單位</th><th>計畫起日</th><th>計畫迄日</th><th>操作</th>
</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<a class="button secondary" href="/research">返回計劃檢視</a></section>""", active="research")


def research_human_page(session: LocalSession, row: Project,
                        people: list[HumanPerson], is_close: str,
                        start: str = "", end: str = "") -> bytes:
    checkboxes = "".join(
        f'<label class="check"><input type="checkbox" name="personKey" '
        f'value="{esc(person.key)}" '
        'onchange="if(this.checked) document.querySelectorAll(\'input[name=personKey]\').forEach('
        'x => { if(x !== this) x.checked = false; });">'
        f'{esc(person.label)}</label>' for person in people)
    return page("列印甲式", f"""{research_portal_form(True)}
<section class="card"><h2>3. 列印甲式</h2>
<p>計畫：{esc(row.number)}（第 {esc(row.period)} 期）{esc(row.name)}</p>
<form method="post" action="/research/human-print">
<input type="hidden" name="csrf" value="{esc(session.csrf)}">
<input type="hidden" name="isClose" value="{esc(is_close)}">
<input type="hidden" name="budgetId" value="{esc(row.budget_id)}">
<input type="hidden" name="number" value="{esc(row.number)}">
<input type="hidden" name="period" value="{esc(row.period)}">
<div class="grid"><div><label for="salaryStartDate">起始年月（民國五碼）</label>
<input id="salaryStartDate" name="salaryStartDate" inputmode="numeric" maxlength="5"
pattern="[0-9]{{5}}" placeholder="例如 11511" value="{esc(start)}" required></div>
<div><label for="salaryEndDate">結束年月（民國五碼）</label>
<input id="salaryEndDate" name="salaryEndDate" inputmode="numeric" maxlength="5"
pattern="[0-9]{{5}}" placeholder="例如 11511" value="{esc(end)}" required></div></div>
<p>勾選一位甲式人員：</p>{checkboxes}
<button type="submit">列印甲式</button></form>
<form method="post" action="/research/list" class="inline">
<input type="hidden" name="csrf" value="{esc(session.csrf)}">
<input type="hidden" name="isClose" value="{esc(is_close)}">
<button type="submit">返回計畫清單</button></form></section>""", active="research")


def business_report_list_page(session: LocalSession, rows: list) -> bytes:
    headers = "".join(f"<th>{esc(field)}</th>" for field in DISPLAY_FIELDS) + "<th>操作</th>"
    body = []
    for row in rows:
        cells = "".join(f"<td>{esc(row.values.get(field, '') or '—')}</td>"
                        for field in DISPLAY_FIELDS)
        if row.print_url:
            action = (f'<form method="post" action="/business-record/print" class="inline">'
                      f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
                      f'<input type="hidden" name="number" value="{esc(row.number)}">'
                      '<button type="submit">列印</button></form>')
        else:
            action = "—"
        body.append(f"<tr>{cells}<td>{action}</td></tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(DISPLAY_FIELDS) + 1}">目前沒有公差紀錄。</td></tr>')
    return page("公差紀錄", f"""<section class="card"><h2>公差紀錄</h2>
<div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<a class="button secondary" href="/">返回公差申請頁</a></section>""")


def list_page(session: LocalSession, roc_date: str, rows: list, raw_html: bytes) -> bytes:
    filtered = rows_for_start_date(rows, roc_date) if roc_date else rows
    columns = "".join(f"<th>{esc(name)}</th>" for name in FIELDS)
    columns += "<th>操作</th>"
    body = []
    for row in filtered:
        cells = "".join(f"<td>{esc(row.values[name] or '—')}</td>" for name in FIELDS)
        try:
            selected_submit_form(row, row.values["起日"])
        except ValueError:
            action = "—"
        else:
            action = (f'<form method="post" action="/submit" class="inline">'
                      f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
                      f'<input type="hidden" name="date" value="{esc(row.values["起日"])}">'
                      f'<input type="hidden" name="applicationId" value="{esc(row.application_id)}">'
                      '<button type="submit">送審</button></form>')
        body.append(f"<tr>{cells}<td>{action}</td></tr>")
    diagnostic = ""
    if not rows:
        detail = list_html_diagnostics(raw_html)
        diagnostic = '<p class="warn">目前無法解析清單，已停用送審。結構計數：' + esc(detail) + '</p>'
    elif not filtered:
        diagnostic = '<p class="warn">目前載入的清單中沒有此起日；如果網站有分頁，請先在官方頁面核對。</p>'
    heading = f"起日 {roc_date} 的申請" if roc_date else "目前清單中的全部申請"
    content = (f'<section class="card"><h2>{esc(heading)}</h2>'
               f'<p>目前載入 {len(rows)} 筆；符合 {len(filtered)} 筆。查詢不會送審。</p>{diagnostic}'
               f'<div class="scroll"><table><thead><tr>{columns}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
               '<p><a class="button secondary" href="/">返回</a></p></section>')
    return page("查詢結果", content)


def appropriation_page(values: dict[str, str | bool] | None = None,
                       message: str = "", connected: bool = False) -> bytes:
    values = values or {}
    def field(name: str, label: str, maxlength: int = 100) -> str:
        value = esc(values.get(name, ""))
        return (f'<div><label for="app-{name}">{esc(label)}</label>'
                f'<input id="app-{name}" name="{name}" data-field="{name}" maxlength="{maxlength}" value="{value}"></div>')

    fields = "".join((
        field("plan_sno", "計畫流水號", 30),
        field("remarks", "備註", 255),
        field("title", "職稱", 30),
        field("name", "姓名", 50),
        field("staff_personal_no", "身分證字號", 10),
        field("list_date", "年月（民國五碼）", 5),
        field("ought_money", "個人應領金額", 10),
        field("health2_money", "個人補充保費", 10),
        field("actual_money", "實發金額", 10),
        field("postoffice_no1", "郵局局號", 20),
        field("postoffice_no2", "郵局帳號", 20),
    ))
    notice = f'<p class="warn">{esc(message)}</p>' if message else ""
    return page("主持人費", f'''{notice}<section class="card"><h2>1. Session</h2>
<form method="post" action="/connect" autocomplete="off"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="returnTo" value="/appropriation">
<label for="appropriation-portal-cookie"></label><p class="note">主要包含 ① portal ② SESSION ③ XSRF-TOKEN。</p>
<input id="appropriation-portal-cookie" name="portalCookie" type="password" autocomplete="off" spellcheck="false">
<button type="submit">手動更新</button></form>
<form method="post" action="/auto-connect" class="inline"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="returnTo" value="/appropriation"><button type="submit">自動更新</button></form>
{session_saved_status(connected)}</section>
<section class="card"><h2>2. 填寫主持人費</h2>
<a class="button secondary" href="/appropriation?load=input02">預設資料 (input02.json)</a>
<form method="post" action="/appropriation/add" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<div id="appropriation-fields"><div class="grid">
<div><label for="app-pay_dept_no">報帳單位代碼（如 T430 資管系）</label>
<input id="app-pay_dept_no" name="pay_dept_no" data-field="pay_dept_no" maxlength="10" value="{esc(values.get('pay_dept_no', ''))}"></div>{fields}
<div><label for="app-address">戶籍地址</label><textarea id="app-address" name="address" data-field="address">{esc(values.get("address", ""))}</textarea></div>
<div><label for="app-memo">工作紀要及時間（限 255 字元）</label><textarea id="app-memo" name="memo" data-field="memo" maxlength="255">{esc(values.get("memo", ""))}</textarea></div>
</div><p><label class="check"><input type="checkbox" name="isAccumulate" data-field="isAccumulate" {"checked" if values.get("isAccumulate") else ""}>是否累計獎金</label></p>
<p><label class="check"><input type="checkbox" name="is_techfund" data-field="is_techfund" {"checked" if values.get("is_techfund") else ""}>國科會經費</label></p>
</div><button type="submit">送出</button></form>
</section><section class="card"><h2>3. 列印印領清冊單</h2>
<form method="post" action="/appropriation/list"><input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="page" value="1"><button type="submit">列出已填寫</button></form>
</section>''', active="appropriation")


def appropriation_list_page(session: LocalSession, rows: list, pages: set[int],
                            current_page: int) -> bytes:
    body = []
    for row in rows:
        action = (f'<form method="post" action="/appropriation/print" class="inline">'
                  f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
                  f'<input type="hidden" name="page" value="{current_page}">'
                  f'<input type="hidden" name="number" value="{esc(row.number)}">'
                  '<button type="submit">列印</button></form>')
        body.append("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in (
            row.number, row.payment_date, row.created_date, row.plan_number,
            row.remarks)) + f"<td>{action}</td></tr>")
    if not body:
        body.append('<tr><td colspan="6">本頁沒有清冊。</td></tr>')
    page_buttons = "".join(
        f'<button type="submit" name="page" value="{number}">{number}</button>'
        for number in sorted(pages) if number != current_page)
    return page("已填寫主持人費", f'''<section class="card"><h2>已填寫主持人費</h2>
<p>第 {current_page} 頁；本頁 {len(rows)} 筆。</p>
<div class="scroll"><table><thead><tr><th>序號</th><th>預計撥款日期</th><th>製表日期</th>
<th>計畫流水號</th><th>備註</th><th>操作</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>
<form method="post" action="/appropriation/list"><input type="hidden" name="csrf" value="{esc(session.csrf)}">
{page_buttons}</form><a class="button secondary" href="/appropriation">返回主持人費</a></section>''',
                active="appropriation")


def memo_page(memos: list[dict[str, str]]) -> bytes:
    rows = []
    for item in memos:
        system = esc(item["system"])
        account = esc(item["account"])
        rows.append(f'''<tr><td>{system}</td><td>{account}</td><td class="memo-password">********</td><td>
<form method="post" action="/memo/reveal" class="inline memo-reveal">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="originalSystem" value="{system}">
<input type="hidden" name="originalAccount" value="{account}"><button type="button">顯示</button></form>
<form method="post" action="/memo/edit" class="inline">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="originalSystem" value="{system}">
<input type="hidden" name="originalAccount" value="{account}"><button type="submit" class="secondary">修改</button></form>
<form method="post" action="/memo/delete" class="inline">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="originalSystem" value="{system}">
<input type="hidden" name="originalAccount" value="{account}"><button type="submit" class="danger">刪除</button></form>
</td></tr>''')
    listing = (f'<div class="scroll"><table><thead><tr><th>系統</th><th>帳號</th><th>密碼</th><th>操作</th></tr>'
               f'</thead><tbody>{"".join(rows)}</tbody></table></div>' if rows
               else '<p class="note">目前沒有備忘錄。</p>')
    return page("備忘錄", f'''<section class="card">
<form method="post" action="/memo/add" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<div class="grid"><div class="memo-system"><label for="memo-system">系統：</label><input id="memo-system" name="system" maxlength="100" required></div>
<div><label for="memo-account">帳號：</label><input id="memo-account" name="account" maxlength="100" required></div>
<div><label for="memo-password">密碼：</label><input id="memo-password" name="password" type="password" maxlength="500" autocomplete="new-password" required></div></div>
<button type="submit">新增</button></form></section>
<section class="card"><h2>已保存項目</h2>{listing}</section>
<script src="/memo.js" defer></script>''', active="memo")


def memo_detail_page(item: dict[str, str]) -> bytes:
    system = esc(item["system"])
    account = esc(item["account"])
    content = f'''<section class="card"><h2>修改：{system}／{account}</h2>
<form method="post" action="/memo/update" autocomplete="off">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="originalSystem" value="{system}">
<input type="hidden" name="originalAccount" value="{account}">
<label for="edit-system">系統：</label><input id="edit-system" name="system" value="{system}" maxlength="100" required>
<label for="edit-account">帳號：</label><input id="edit-account" name="account" value="{account}" maxlength="100" required>
<label for="edit-password">密碼：（留空表示不更改）</label>
<input id="edit-password" name="password" type="password" maxlength="500" autocomplete="new-password">
<button type="submit">儲存修改</button><a class="button secondary" href="/memo">取消</a></form></section>'''
    return page("備忘錄", content, active="memo")


class Handler(BaseHTTPRequestHandler):
    server_version = "HumanSysLocalWeb/1"

    def log_message(self, format: str, *args) -> None:  # type: ignore[no-untyped-def]
        # Never print paths, cookies, form fields, or network errors to logs.
        return

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (
            f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"
        )

    def _send(self, body: bytes, status: int = 200, cookie: str | None = None,
              content_type: str = "text/html; charset=utf-8") -> None:
        cookie = cookie or getattr(self, "_response_cookie", None)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _local_session(self) -> tuple[str, LocalSession] | None:
        now = time.monotonic()
        for key in list(SESSIONS):
            if SESSIONS[key].expires < now:
                del SESSIONS[key]
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("hs_local_session")
        if morsel is None:
            return None
        key = morsel.value
        session = SESSIONS.get(key)
        if session:
            session.expires = now + SESSION_TTL
            return key, session
        return None

    def _account_session(self) -> tuple[str, dict[str, str]] | None:
        now = time.monotonic()
        for key in list(ACCOUNT_SESSIONS):
            if ACCOUNT_SESSIONS[key][1] < now:
                del ACCOUNT_SESSIONS[key]
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("acc_local_session")
        if morsel is None or morsel.value not in ACCOUNT_SESSIONS:
            return None
        values, _ = ACCOUNT_SESSIONS[morsel.value]
        ACCOUNT_SESSIONS[morsel.value] = (values, now + SESSION_TTL)
        return morsel.value, values

    def _account_session_values(self) -> dict[str, str] | None:
        found = self._account_session()
        return found[1] if found else None

    def _local_account_session(self) -> bool:
        return self._account_session_values() is not None

    def _research_session(self) -> tuple[str, LocalSession] | None:
        now = time.monotonic()
        for key in list(RESEARCH_SESSIONS):
            if RESEARCH_SESSIONS[key].expires < now:
                del RESEARCH_SESSIONS[key]
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("research_local_session")
        if morsel is None:
            return None
        session = RESEARCH_SESSIONS.get(morsel.value)
        if session:
            session.expires = now + SESSION_TTL
            return morsel.value, session
        return None

    def _appropriation_session(self) -> tuple[str, LocalSession] | None:
        now = time.monotonic()
        for key in list(APPROPRIATION_SESSIONS):
            if APPROPRIATION_SESSIONS[key].expires < now:
                del APPROPRIATION_SESSIONS[key]
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("app_local_session")
        if morsel is None:
            return None
        session = APPROPRIATION_SESSIONS.get(morsel.value)
        if session:
            session.expires = now + SESSION_TTL
            return morsel.value, session
        return None

    def _read_form(self) -> tuple[dict[str, str], tuple[str, bytes] | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("請求大小不正確。") from exc
        if length <= 0 or length > MAX_POST_BYTES:
            raise ValueError("請求為空或超過 6 MB。")
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        fields: dict[str, str] = {}
        upload: tuple[str, bytes] | None = None
        if content_type.startswith("application/x-www-form-urlencoded"):
            values = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError("表單欄位重複，已停止。")
            fields = {key: value[0] for key, value in values.items()}
        elif content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii")
                + b"\r\n\r\n" + raw
            )
            if not message.is_multipart():
                raise ValueError("無法解析上傳表單。")
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not isinstance(name, str):
                    continue
                payload = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                if filename:
                    if name != "proofFile" or upload is not None:
                        raise ValueError("只能上傳一個證明文件。")
                    if len(payload) > MAX_ATTACHMENT_BYTES:
                        raise ValueError("上傳附件超過 4 MB。")
                    upload = (filename, payload)
                elif name in fields:
                    raise ValueError("表單欄位重複，已停止。")
                else:
                    fields[name] = payload.decode("utf-8")
        else:
            raise ValueError("不支援的表單格式。")
        return fields, upload

    def do_GET(self) -> None:
        if not self._host_ok() or self.path not in ("/", "/?load=input", "/account", "/account?load=input01", "/account/personnel", "/account/personnel?load=input03", "/research", "/appropriation", "/appropriation?load=input02", "/memo", "/memo.js"):
            self._send(page("無法開啟", "<p>只允許本機功能頁面。</p>"), HTTPStatus.NOT_FOUND)
            return
        if self.path in ("/appropriation", "/appropriation?load=input02"):
            try:
                values = load_appropriation_defaults() if self.path.endswith("?load=input02") else None
                self._send(appropriation_page(values, connected=self._appropriation_session() is not None))
            except ValueError as exc:
                self._send(page("設定錯誤", f'<p class="warn">{esc(exc)}</p>', active="appropriation"), 500)
            return
        if self.path == "/memo.js":
            self._send((Path(__file__).parent / "browser" / "memo.js").read_bytes(),
                       content_type="application/javascript; charset=utf-8")
            return
        if self.path == "/memo":
            try:
                self._send(memo_page(load_password_memos()))
            except ValueError as exc:
                self._send(page("備忘錄錯誤", f'<p class="warn">{esc(exc)}</p>', active="memo"), 500)
            return
        if self.path == "/research":
            found = self._research_session()
            self._send(research_page(found[1] if found else None))
            return
        if self.path in ("/account/personnel", "/account/personnel?load=input03"):
            try:
                values = load_personnel_defaults() if self.path.endswith("?load=input03") else None
                self._send(personnel_page(values=values, connected=self._local_account_session()))
            except ValueError as exc:
                self._send(page("設定錯誤", f'<p class="warn">{esc(exc)}</p>', active="personnel"), 500)
            return
        if self.path in ("/account", "/account?load=input01"):
            try:
                values = load_account_defaults() if self.path.endswith("?load=input01") else None
                self._send(account_page(self._local_account_session(), values=values))
            except ValueError as exc:
                self._send(page("設定錯誤", f'<p class="warn">{esc(exc)}</p>'), 500)
            return
        found = self._local_session()
        try:
            self._send(dashboard(found[1] if found else None,
                                 load_input=self.path == "/?load=input"))
        except ValueError as exc:
            self._send(page("設定錯誤", f'<p class="warn">{esc(exc)}</p>'), 500)

    def do_POST(self) -> None:
        self._appropriation_progress = ["idle"]
        if not self._host_ok():
            local_url = f"http://127.0.0.1:{self.server.server_port}/"
            self._send(page("拒絕請求", f"<p>請從 {esc(local_url)} 開啟本機網頁。</p>"), 403)
            return
        if self.path not in {
            "/account/connect", "/account/auto-connect", "/account/save", "/account/list", "/account/print-pdf",
            "/account/personnel/prefill", "/account/personnel/save",
            "/account/prefill", "/connect", "/auto-connect", "/logout", "/draft", "/list", "/submit",
            "/business-record/list", "/business-record/print",
            "/research/connect", "/research/list", "/research/print",
            "/research/human", "/research/human-print",
            "/appropriation/add", "/appropriation/list", "/appropriation/print",
            "/memo/add", "/memo/reveal", "/memo/edit", "/memo/update", "/memo/delete",
        }:
            self._send(page("找不到", "<p>不支援的操作。</p>"), 404)
            return
        try:
            form, upload = self._read_form()
            found = self._local_session()
            research_found = self._research_session() if self.path.startswith("/research/") else None
            appropriation_found = (self._appropriation_session()
                                   if self.path.startswith("/appropriation/") else None)
            submitted_csrf = form.get("csrf", "")
            if (not secrets.compare_digest(submitted_csrf, START_CSRF)
                    and not (found and secrets.compare_digest(submitted_csrf, found[1].csrf))
                    and not (research_found and secrets.compare_digest(
                        submitted_csrf, research_found[1].csrf))
                    and not (appropriation_found and secrets.compare_digest(
                        submitted_csrf, appropriation_found[1].csrf))):
                raise ValueError("本機表單驗證失敗；沒有執行任何操作。")
            if self.path.startswith("/memo/"):
                if upload is not None or not secrets.compare_digest(submitted_csrf, START_CSRF):
                    raise ValueError("本機表單驗證失敗；沒有修改備忘錄。")
                if self.path in ("/memo/add", "/memo/update", "/memo/delete"):
                    operation = self.path.rsplit("/", 1)[1]
                    update_password_memos(operation, form)
                    self._send(memo_page(load_password_memos()))
                    return
                memos = load_password_memos()
                matches = [item for item in memos
                           if item["system"] == form.get("originalSystem", "")
                           and item["account"] == form.get("originalAccount", "")]
                if len(matches) != 1:
                    raise ValueError("找不到指定的系統與帳號；沒有執行。")
                if self.path == "/memo/reveal":
                    self._send(json.dumps({"password": matches[0]["password"]},
                                          ensure_ascii=False).encode("utf-8"),
                               content_type="application/json; charset=utf-8")
                else:
                    self._send(memo_detail_page(matches[0]))
                return
            if self.path == "/appropriation/add":
                if upload is not None or not appropriation_found:
                    raise ValueError("請先在本頁更新 Portal Session 並連線；沒有加入。")
                key, session = appropriation_found
                validate_appropriation_values(form)
                before, _ = fetch_appropriation_list(session.opener, 1)
                previous_numbers = {row.number for row in before}
                payload = appropriation_add_payload(session.opener, form)
                fingerprint = hashlib.sha256(payload).hexdigest()
                attempt_key = (key, fingerprint)
                now = time.monotonic()
                for old_key, until in list(APPROPRIATION_ADD_ATTEMPTS.items()):
                    if until < now:
                        del APPROPRIATION_ADD_ATTEMPTS[old_key]
                if attempt_key in APPROPRIATION_ADD_ATTEMPTS:
                    raise ValueError("15 分鐘內已嘗試加入相同資料；請先查官方清單，勿直接重試。")
                APPROPRIATION_ADD_ATTEMPTS[attempt_key] = now + ACCOUNT_RETRY_GUARD_SECONDS
                number = send_appropriation_once(session.opener, payload, form,
                                                previous_numbers,
                                                self._appropriation_progress)
                message = (f"已送出並在官方清單找到新紀錄：{number}。" if number else
                           "已執行官方「加入」與「送出」，但清單未確認到唯一新紀錄；"
                           "請先在官方頁核對，勿直接重試。")
                self._send(appropriation_page(message=message, connected=True))
                return
            if self.path in ("/appropriation/list", "/appropriation/print"):
                if upload is not None or not appropriation_found:
                    raise ValueError("請先在本頁更新 Portal Session 並連線；沒有列印。")
                _, session = appropriation_found
                page_text = form.get("page", "1")
                if not re.fullmatch(r"[1-9][0-9]{0,3}", page_text):
                    raise ValueError("清單頁碼不正確；沒有列印。")
                page_number = int(page_text)
                rows, pages = fetch_appropriation_list(session.opener, page_number)
                if self.path == "/appropriation/list":
                    self._send(appropriation_list_page(session, rows, pages, page_number))
                    return
                number = form.get("number", "")
                matches = [row for row in rows if row.number == number]
                if len(matches) != 1:
                    raise ValueError("本頁找不到唯一的指定清冊；沒有列印。")
                saved = save_appropriation_pdf(session.opener, matches[0])
                self._send(page("已儲存 PDF", f'''<section class="card"><h2>PDF 已儲存</h2>
<p>清冊序號：{esc(number)}</p><p>位置：{esc(saved)}</p>
<a class="button secondary" href="/appropriation">返回主持人費</a></section>''',
                                active="appropriation"))
                return
            if self.path == "/research/connect":
                if submitted_csrf != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有連線。")
                raw_cookie = form.get("portalCookie", "").strip()
                portal_cookie = (portal_cookie_from_header(raw_cookie) if raw_cookie
                                 else saved_portal_cookie())
                if not portal_cookie:
                    raise ValueError("請貼上 Portal Cookie，或先保存有效的 Session。")
                if raw_cookie:
                    save_json(SESSION_JSON, {"portalCookie": portal_cookie}, private=True)
                    SESSIONS.clear()
                    RESEARCH_SESSIONS.clear()
                opener, _ = bootstrap_research_from_portal(portal_cookie)
                session = LocalSession(opener, secrets.token_urlsafe(32),
                                       time.monotonic() + SESSION_TTL)
                key = secrets.token_urlsafe(32)
                if len(RESEARCH_SESSIONS) >= 20:
                    RESEARCH_SESSIONS.pop(next(iter(RESEARCH_SESSIONS)))
                RESEARCH_SESSIONS[key] = session
                cookie = (f"research_local_session={key}; HttpOnly; SameSite=Strict; "
                          f"Path=/; Max-Age={SESSION_TTL}")
                self._send(research_page(session), cookie=cookie)
                return
            if self.path in ("/research/list", "/research/print",
                             "/research/human", "/research/human-print"):
                if upload is not None:
                    raise ValueError("不接受附件；沒有列印。")
                is_close = form.get("isClose", "")
                if is_close not in ("", "N", "Y"):
                    raise ValueError("結案篩選值不正確；沒有列出計劃。")
                if self.path == "/research/list":
                    if research_found:
                        if not secrets.compare_digest(submitted_csrf, research_found[1].csrf):
                            raise ValueError("本機表單驗證失敗；沒有列出計劃。")
                        session = research_found[1]
                        rows = fetch_projects(session.opener, is_close)
                        cookie = None
                    else:
                        if submitted_csrf != START_CSRF:
                            raise ValueError("本機表單驗證失敗；沒有列出計劃。")
                        portal_cookie = saved_portal_cookie()
                        if not portal_cookie:
                            raise ValueError("請先在上方更新 Portal Session。")
                        opener, default_rows = bootstrap_research_from_portal(portal_cookie)
                        session = LocalSession(opener, secrets.token_urlsafe(32),
                                               time.monotonic() + SESSION_TTL)
                        key = secrets.token_urlsafe(32)
                        if len(RESEARCH_SESSIONS) >= 20:
                            RESEARCH_SESSIONS.pop(next(iter(RESEARCH_SESSIONS)))
                        RESEARCH_SESSIONS[key] = session
                        cookie = (f"research_local_session={key}; HttpOnly; SameSite=Strict; "
                                  f"Path=/; Max-Age={SESSION_TTL}")
                        rows = default_rows if is_close == "" else fetch_projects(opener, is_close)
                    self._send(research_list_page(session, rows, is_close), cookie=cookie)
                    return
                if (research_found is None or not secrets.compare_digest(
                    submitted_csrf, research_found[1].csrf
                )):
                    raise ValueError("請先按「列出計劃」取得清單；沒有列印。")
                rows = fetch_projects(research_found[1].opener, is_close)
                budget_id = form.get("budgetId", "")
                number = form.get("number", "")
                period = form.get("period", "")
                matches = [row for row in rows if row.budget_id == budget_id
                           and row.number == number and row.period == period]
                if len(matches) != 1:
                    raise ValueError("官方清單中找不到唯一且相符的計畫期別；沒有列印。")
                if self.path == "/research/human":
                    people = fetch_human_people(research_found[1].opener, budget_id)
                    self._send(research_human_page(research_found[1], matches[0],
                                                   people, is_close))
                    return
                if self.path == "/research/human-print":
                    saved = save_human_a_pdf(
                        research_found[1].opener, budget_id,
                        form.get("personKey", ""), form.get("salaryStartDate", ""),
                        form.get("salaryEndDate", ""))
                    self._send(page("已儲存甲式", f"""<section class="card"><h2>甲式 PDF 已儲存</h2>
<p>計畫流水號：{esc(number)}；期別：{esc(period)}</p><p>位置：{esc(saved)}</p>
<form method="post" action="/research/list" class="inline">
<input type="hidden" name="csrf" value="{esc(research_found[1].csrf)}">
<input type="hidden" name="isClose" value="{esc(is_close)}">
<button type="submit">返回計畫清單</button></form></section>""", active="research"))
                    return
                saved = save_budget_pdf(research_found[1].opener, matches[0])
                self._send(page("已儲存經分表", f"""<section class="card"><h2>經分表 PDF 已儲存</h2>
<p>計畫流水號：{esc(number)}；期別：{esc(period)}</p><p>位置：{esc(saved)}</p>
<form method="post" action="/research/list" class="inline">
<input type="hidden" name="csrf" value="{esc(research_found[1].csrf)}">
<input type="hidden" name="isClose" value="{esc(is_close)}">
<button type="submit">返回計畫清單</button></form>
<a class="button secondary" href="/research">返回計劃檢視</a></section>""", active="research"))
                return
            if self.path == "/account/auto-connect":
                if submitted_csrf != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有自動登入。")
                return_to = form.get("returnTo", "")
                if return_to not in ("", "/account/personnel"):
                    raise ValueError("返回頁面不正確；沒有自動登入。")
                values = automatic_account_cookies()
                verify_account_cookies(values)
                save_json(ACCOUNT_SESSION_JSON,
                          {"accountCookie": account_cookie_header(values)}, private=True)
                key = secrets.token_urlsafe(32)
                if len(ACCOUNT_SESSIONS) >= 20:
                    ACCOUNT_SESSIONS.pop(next(iter(ACCOUNT_SESSIONS)))
                ACCOUNT_SESSIONS[key] = (values, time.monotonic() + SESSION_TTL)
                cookie = (f"acc_local_session={key}; HttpOnly; SameSite=Strict; "
                          f"Path=/; Max-Age={SESSION_TTL}")
                connected_page = (personnel_page(connected=True)
                                  if return_to == "/account/personnel"
                                  else account_page(True))
                self._send(connected_page, cookie=cookie)
                return
            if self.path == "/account/connect":
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有連線。")
                return_to = form.get("returnTo", "")
                if return_to not in ("", "/account/personnel"):
                    raise ValueError("返回頁面不正確；沒有連線。")
                raw_cookie = form.get("accountCookie", "").strip()
                if raw_cookie:
                    values = account_cookies_from_header(raw_cookie)
                else:
                    values = saved_account_cookies()
                    if not values:
                        raise ValueError("請貼上報帳 Cookie，或先保存有效的 Session。")
                verify_account_cookies(values)
                if raw_cookie:
                    save_json(ACCOUNT_SESSION_JSON,
                              {"accountCookie": account_cookie_header(values)}, private=True)
                key = secrets.token_urlsafe(32)
                if len(ACCOUNT_SESSIONS) >= 20:
                    ACCOUNT_SESSIONS.pop(next(iter(ACCOUNT_SESSIONS)))
                ACCOUNT_SESSIONS[key] = (values, time.monotonic() + SESSION_TTL)
                cookie = (f"acc_local_session={key}; HttpOnly; SameSite=Strict; "
                          f"Path=/; Max-Age={SESSION_TTL}")
                connected_page = (personnel_page(connected=True)
                                  if return_to == "/account/personnel"
                                  else account_page(True))
                self._send(connected_page, cookie=cookie)
                return
            if self.path == "/account/save":
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有存入。")
                account_session = self._account_session()
                if account_session is None:
                    raise ValueError("請先更新報帳 Session 並連線；沒有存入。")
                record = account_record_from_form(form)
                validate_account_record(record)
                fingerprint = hashlib.sha256(
                    json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                retry_key = (account_session[0], fingerprint)
                now = time.monotonic()
                for key, expires in list(ACCOUNT_SAVE_ATTEMPTS.items()):
                    if expires < now:
                        del ACCOUNT_SAVE_ATTEMPTS[key]
                if retry_key in ACCOUNT_SAVE_ATTEMPTS:
                    raise ValueError("同一 Session 已在 15 分鐘內嘗試存入相同資料；請先到官方清單確認，勿直接重試。")
                context = ssl.create_default_context()
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
                try:
                    prepared = prepare_save(record, account_session[1], context)
                except OffsiteRedirect as exc:
                    raise ValueError("官方表單導向其他網域；沒有存入。") from exc
                # Reserve before network I/O. Even an unclear response may have saved.
                ACCOUNT_SAVE_ATTEMPTS[retry_key] = time.monotonic() + ACCOUNT_RETRY_GUARD_SECONDS
                result = send_save_once(prepared)
                detail = ("官方回應包含資料已存入" if result.success_text_found
                          else "無法從官方回應確認是否已存入")
                self._send(page("已發出一次存入請求", f"""<section class="card">
<h2>已發出一次報帳存入請求</h2><p>HTTP {esc(result.status)}；回應路徑 {esc(result.path)}。</p>
<p>{esc(detail)}。</p><p class="warn">請到官方報帳清單確認是否新增紀錄；無論畫面結果如何，
都不要直接重試，以免重複建立。這一步只存入，沒有執行其他送審操作。</p>
<a class="button secondary" href="/account">返回報帳頁</a></section>""", active="account"))
                return
            if self.path in ("/account/list", "/account/print-pdf"):
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有列出案件或產生 PDF。")
                account_session = self._account_session()
                if account_session is None:
                    raise ValueError("請先更新報帳 Session 並連線；沒有列出案件或產生 PDF。")
                year_text = form.get("year", "").strip()
                if not re.fullmatch(r"\d{2,3}", year_text):
                    raise ValueError("請輸入正確民國年份；沒有列出案件或產生 PDF。")
                category = form.get("category", "14")
                if category not in ("14", "15"):
                    raise ValueError("報支類別不正確；沒有列出案件或產生 PDF。")
                if self.path == "/account/print-pdf":
                    number = form.get("number", "").strip()
                    if not re.fullmatch(r"\d{1,20}", number):
                        raise ValueError("請輸入正確請購單號；沒有產生 PDF。")
                    addition = form.get("addition", "")
                    if not re.fullmatch(r"[A-Za-z0-9]{0,20}", addition):
                        raise ValueError("請購附號不正確；沒有產生 PDF。")
                context = ssl.create_default_context()
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
                try:
                    rows = fetch_purchase_list(account_session[1], context, int(year_text),
                                               category)
                except OffsiteRedirect as exc:
                    raise ValueError("官方購案管理頁導向其他網域；沒有列出案件或產生 PDF。") from exc
                if self.path == "/account/list":
                    self._send(account_purchase_list_page(rows, int(year_text), category))
                    return
                row = find_purchase(rows, number)
                if row.addition != addition:
                    raise ValueError("官方清單中的附號已變更；沒有產生 PDF。")
                if row.category != category:
                    raise ValueError("官方清單中的類別已變更；沒有產生 PDF。")
                try:
                    saved = save_purchase_pdf(row, int(year_text), account_session[1], context)
                except OffsiteRedirect as exc:
                    raise ValueError("官方列印頁導向其他網域；沒有產生 PDF。") from exc
                self._send(page("已儲存 PDF", f"""<section class="card"><h2>PDF 已儲存</h2>
<p>請購單號：{esc(number)}</p><p>位置：{esc(saved)}</p>
<form method="post" action="/account/list" class="inline">
<input type="hidden" name="csrf" value="{esc(START_CSRF)}">
<input type="hidden" name="year" value="{esc(year_text)}">
<input type="hidden" name="category" value="{category}">
<button type="submit">返回案件清單</button></form>
<a class="button secondary" href="{'/account/personnel' if category == '15' else '/account'}">返回報帳頁</a></section>""",
                                active="personnel" if category == "15" else "account"))
                return
            if self.path == "/account/prefill":
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有產生腳本。")
                record = account_record_from_form(form)
                validate_account_record(record)
                script = account_prefill_script(record)
                self._send(account_page(self._local_account_session(),
                                        "欄位檢查通過；尚未存入報帳系統。", record,
                                        prefill_script=script))
                return
            if self.path == "/account/personnel/prefill":
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有產生腳本。")
                record = personnel_record_from_form(form)
                validate_personnel_record(record)
                script = personnel_prefill_script(record)
                self._send(personnel_page("欄位檢查通過；尚未存入報帳系統。",
                                          record, script, self._local_account_session()))
                return
            if self.path == "/account/personnel/save":
                if form.get("csrf") != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有存入。")
                account_session = self._account_session()
                if account_session is None:
                    raise ValueError("請先更新報帳 Session 並連線；沒有存入。")
                record = personnel_record_from_form(form)
                validate_personnel_record(record)
                script = personnel_prefill_script(record)
                fingerprint = hashlib.sha256(
                    json.dumps({"kind": "personnel", "record": record},
                               ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                retry_key = (account_session[0], fingerprint)
                now = time.monotonic()
                for key, expires in list(ACCOUNT_SAVE_ATTEMPTS.items()):
                    if expires < now:
                        del ACCOUNT_SAVE_ATTEMPTS[key]
                if retry_key in ACCOUNT_SAVE_ATTEMPTS:
                    raise ValueError("同一 Session 已在 15 分鐘內嘗試存入相同人事費資料；請先到官方清單確認，勿直接重試。")
                context = ssl.create_default_context()
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
                prepared = prepare_personnel_save(record, account_session[1], context, script)
                # Reserve before the only final network POST. Unclear outcomes also block retries.
                ACCOUNT_SAVE_ATTEMPTS[retry_key] = time.monotonic() + ACCOUNT_RETRY_GUARD_SECONDS
                result = send_save_once(prepared)
                detail = ("官方回應包含資料已存入" if result.success_text_found
                          else "無法從官方回應確認是否已存入")
                self._send(page("已發出一次存入請求", f"""<section class="card">
<h2>已發出一次人事費報帳存入請求</h2><p>HTTP {esc(result.status)}；回應路徑 {esc(result.path)}。</p>
<p>{esc(detail)}。</p><p class="warn">請到官方報帳清單確認是否新增紀錄；
結果不明時不要直接重試，以免重複建立。這一步只存入，沒有執行其他送審操作。</p>
<a class="button secondary" href="/account/personnel">返回主持人費報帳頁</a></section>""",
                                active="personnel"))
                return
            if self.path in ("/business-record/list", "/business-record/print"):
                if upload is not None or found is None or not secrets.compare_digest(
                    submitted_csrf, found[1].csrf
                ):
                    raise ValueError("請先更新 Portal Session 並連線；沒有列印。")
                session = found[1]
                if self.path == "/business-record/print":
                    number = form.get("number", "").strip()
                    if not re.fullmatch(r"\d{1,20}", number):
                        raise ValueError("申請單號格式不正確；沒有列印。")
                rows = fetch_report(session)
                if self.path == "/business-record/list":
                    self._send(business_report_list_page(session, rows))
                    return
                matches = [row for row in rows if row.number == number]
                if len(matches) != 1:
                    raise ValueError("官方紀錄中找不到唯一的指定單號；沒有列印。")
                saved = save_report_pdf(session, matches[0])
                self._send(page("已儲存 PDF", f"""<section class="card"><h2>PDF 已儲存</h2>
<p>申請單號：{esc(number)}</p><p>位置：{esc(saved)}</p>
<form method="post" action="/business-record/list" class="inline">
<input type="hidden" name="csrf" value="{esc(session.csrf)}">
<button type="submit">返回公差紀錄</button></form>
<a class="button secondary" href="/">返回公差申請頁</a></section>"""))
                return
            if self.path == "/auto-connect":
                if submitted_csrf != START_CSRF or upload is not None:
                    raise ValueError("本機表單驗證失敗；沒有自動登入。")
                return_to = form.get("returnTo", "")
                if return_to not in ("", "/research", "/appropriation"):
                    raise ValueError("返回頁面不正確；沒有自動登入。")
                portal_cookie = automatic_portal_cookie()
                if return_to == "/research":
                    opener, _ = bootstrap_research_from_portal(portal_cookie)
                    session = LocalSession(opener, secrets.token_urlsafe(32),
                                           time.monotonic() + SESSION_TTL)
                    key = secrets.token_urlsafe(32)
                    if len(RESEARCH_SESSIONS) >= 20:
                        RESEARCH_SESSIONS.pop(next(iter(RESEARCH_SESSIONS)))
                    RESEARCH_SESSIONS[key] = session
                    cookie = (f"research_local_session={key}; HttpOnly; SameSite=Strict; "
                              f"Path=/; Max-Age={SESSION_TTL}")
                    response = research_page(session)
                elif return_to == "/appropriation":
                    opener = bootstrap_appropriation_from_portal(portal_cookie)
                    session = LocalSession(opener, secrets.token_urlsafe(32),
                                           time.monotonic() + SESSION_TTL)
                    if appropriation_form_status(session) != "form":
                        raise ValueError("Portal 尚未取得撥帳新增表單；原 Session 未變更。")
                    key = secrets.token_urlsafe(32)
                    if len(APPROPRIATION_SESSIONS) >= 20:
                        APPROPRIATION_SESSIONS.pop(next(iter(APPROPRIATION_SESSIONS)))
                    APPROPRIATION_SESSIONS[key] = session
                    cookie = (f"app_local_session={key}; HttpOnly; SameSite=Strict; "
                              f"Path=/; Max-Age={SESSION_TTL}")
                    response = appropriation_page(connected=True)
                else:
                    session = bootstrap(portal_cookie)
                    key = secrets.token_urlsafe(32)
                    if len(SESSIONS) >= 20:
                        SESSIONS.pop(next(iter(SESSIONS)))
                    SESSIONS[key] = session
                    cookie = (f"hs_local_session={key}; HttpOnly; SameSite=Strict; "
                              f"Path=/; Max-Age={SESSION_TTL}")
                    response = dashboard(session, "Session 已保存。")
                try:
                    save_json(SESSION_JSON, {"portalCookie": portal_cookie}, private=True)
                except OSError:
                    if return_to == "/research":
                        RESEARCH_SESSIONS.pop(key, None)
                    elif return_to == "/appropriation":
                        APPROPRIATION_SESSIONS.pop(key, None)
                    else:
                        SESSIONS.pop(key, None)
                    raise
                self._send(response, cookie=cookie)
                return
            if self.path == "/connect":
                if form.get("csrf") != START_CSRF:
                    raise ValueError("本機表單驗證失敗。")
                return_to = form.get("returnTo", "")
                if return_to not in ("", "/appropriation"):
                    raise ValueError("返回頁面不正確；沒有連線。")
                raw_cookie = form.get("portalCookie", "").strip()
                if raw_cookie:
                    portal_cookie = portal_cookie_from_header(raw_cookie)
                    previous = self._local_session()
                    if previous:
                        SESSIONS.pop(previous[0], None)
                    save_json(SESSION_JSON, {"portalCookie": portal_cookie}, private=True)
                else:
                    portal_cookie = saved_portal_cookie()
                if not portal_cookie:
                    raise ValueError("請貼上 Portal Cookie，或先保存有效的 Session。")
                if return_to == "/appropriation":
                    opener = bootstrap_appropriation_from_portal(portal_cookie)
                    session = LocalSession(opener, secrets.token_urlsafe(32),
                                           time.monotonic() + SESSION_TTL)
                    if appropriation_form_status(session) != "form":
                        raise ValueError("Portal 尚未取得撥帳新增表單；沒有連線。")
                    key = secrets.token_urlsafe(32)
                    if len(APPROPRIATION_SESSIONS) >= 20:
                        APPROPRIATION_SESSIONS.pop(next(iter(APPROPRIATION_SESSIONS)))
                    APPROPRIATION_SESSIONS[key] = session
                    cookie = (f"app_local_session={key}; HttpOnly; SameSite=Strict; "
                              f"Path=/; Max-Age={SESSION_TTL}")
                    self._send(appropriation_page(connected=True), cookie=cookie)
                    return
                session = bootstrap(portal_cookie)
                key = secrets.token_urlsafe(32)
                if len(SESSIONS) >= 20:
                    SESSIONS.pop(next(iter(SESSIONS)))
                SESSIONS[key] = session
                cookie = f"hs_local_session={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"
                self._send(dashboard(session, "Session 已保存。"), cookie=cookie)
                return
            if found is None:
                if form.get("csrf") != START_CSRF:
                    raise ValueError("工作階段已失效。請返回首頁重新操作；沒有送出。")
                if self.path == "/logout":
                    self._send(dashboard(None, "目前沒有已連線的工作階段。"))
                    return
                portal_cookie = saved_portal_cookie()
                if not portal_cookie:
                    raise ValueError("請先在首頁更新 Portal Session；沒有送出。")
                session = bootstrap(portal_cookie)
                key = secrets.token_urlsafe(32)
                if len(SESSIONS) >= 20:
                    SESSIONS.pop(next(iter(SESSIONS)))
                SESSIONS[key] = session
                self._response_cookie = (f"hs_local_session={key}; HttpOnly; SameSite=Strict; "
                                         f"Path=/; Max-Age={SESSION_TTL}")
            else:
                key, session = found
                submitted_csrf = form.get("csrf", "")
                if (not secrets.compare_digest(submitted_csrf, session.csrf)
                        and not secrets.compare_digest(submitted_csrf, START_CSRF)):
                    raise ValueError("本機表單驗證失敗。")
            if self.path == "/logout":
                SESSIONS.pop(key, None)
                self._send(dashboard(None, "已結束本機工作階段。"),
                           cookie="hs_local_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if self.path == "/draft":
                self._handle_draft(session, form, upload)
            elif self.path == "/list":
                roc_date = form.get("date", "").strip()
                if roc_date:
                    validate_roc_date(roc_date)
                rows, raw = fetch_list(session)
                self._send(list_page(session, roc_date, rows, raw))
            elif self.path == "/submit":
                self._handle_submit(session, form)
            else:
                self._send(page("找不到", "<p>不支援的操作。</p>"), 404)
        except ValueError as exc:
            if self.path == "/account/auto-connect":
                target = form.get("returnTo", "") if "form" in locals() else ""
                back = "/account/personnel" if target == "/account/personnel" else "/account"
                self._send(page("自動更新未完成", f'<section class="card"><h2>自動更新未完成</h2>'
                                f'<p class="warn">{esc(exc)}</p>'
                                f'<a class="button secondary" href="{back}">返回</a></section>',
                                active="personnel" if back == "/account/personnel" else "account"), 400)
                return
            if self.path == "/auto-connect":
                target = form.get("returnTo", "") if "form" in locals() else ""
                back = target if target in ("/research", "/appropriation") else "/"
                active = ("research" if back == "/research" else
                          "appropriation" if back == "/appropriation" else "business")
                self._send(page("自動更新未完成", f'<section class="card"><h2>自動更新未完成</h2>'
                                f'<p class="warn">{esc(exc)}</p>'
                                f'<a class="button secondary" href="{back}">返回</a></section>',
                                active=active), 400)
                return
            if (self.path == "/connect" and "form" in locals()
                    and form.get("returnTo") == "/appropriation"):
                self._send(appropriation_page(message=str(exc)), 400)
                return
            if self.path.startswith("/appropriation/"):
                stage = self._appropriation_progress[0]
                if stage in ("add_response", "added"):
                    message = f"官方已回應「加入」請求（不代表加入成功），「送出」尚未執行：{exc}"
                elif stage in ("submitting", "submit_response", "submitted"):
                    message = "官方「送出」可能已執行；清單尚未確認，請先核對，勿直接重試。"
                elif stage == "adding":
                    message = "官方「加入」結果不明；請先核對，勿直接重試。"
                else:
                    message = str(exc)
                self._send(appropriation_page(values=form if self.path == "/appropriation/add" else None,
                                              message=message,
                                              connected=bool(self._appropriation_session())), 400)
                return
            if self.path.startswith("/memo/"):
                self._send(page("未執行", f'<section class="card"><h2>操作未執行</h2>'
                                f'<p class="warn">{esc(exc)}</p>'
                                '<a class="button secondary" href="/memo">返回備忘錄</a></section>',
                                active="memo"), 400)
                return
            if self.path == "/research/connect":
                self._send(research_page(message=str(exc)), 400)
                return
            back = ("/account/personnel" if self.path in ("/account/connect", "/account/auto-connect")
                    and "form" in locals() and form.get("returnTo") == "/account/personnel" else
                    "/account/personnel" if self.path in ("/account/list", "/account/print-pdf")
                    and "form" in locals() and form.get("category") == "15" else
                    "/research" if self.path.startswith("/research/") else
                    "/account/personnel" if self.path.startswith("/account/personnel/") else
                    "/account" if self.path.startswith("/account/") else "/")
            self._send(page("未執行", f'<section class="card"><h2>操作未執行</h2>'
                            f'<p class="warn">{esc(exc)}</p><a class="button secondary" href="{back}">返回</a></section>',
                            active="personnel" if back == "/account/personnel" else
                            "research" if back == "/research" else
                            "account" if back == "/account" else "business"), 400)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            if (self.path == "/connect" and "form" in locals()
                    and form.get("returnTo") == "/appropriation"):
                self._send(appropriation_page(message="撥帳連線失敗；沒有送出資料。"), 502)
                return
            if self.path.startswith("/appropriation/"):
                stage = self._appropriation_progress[0]
                if stage in ("add_response", "added"):
                    message = "官方已回應「加入」請求（不代表加入成功），「送出」尚未執行；請先核對，勿直接重試。"
                elif stage in ("submitting", "submit_response", "submitted"):
                    message = "官方「送出」結果不明；請先查清單，勿直接重試。"
                elif stage == "adding":
                    message = "官方「加入」結果不明；請先核對，勿直接重試。"
                else:
                    message = "撥帳連線失敗；沒有送出資料。"
                self._send(appropriation_page(values=form if self.path == "/appropriation/add" else None,
                                              message=message,
                                              connected=bool(self._appropriation_session())), 502)
                return
            if self.path.startswith("/memo/"):
                self._send(page("儲存失敗", '<section class="card"><h2>備忘錄未能儲存</h2>'
                                '<p class="warn">請檢查 p@ssw0rd.json 與目錄權限；不要在確認前重試。</p>'
                                '<a class="button secondary" href="/memo">返回備忘錄</a></section>',
                                active="memo"), 500)
                return
            code = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else "連線失敗"
            account_error = self.path.startswith("/account/")
            research_error = self.path.startswith("/research/")
            system = "ResearchSys" if research_error else "報帳系統" if account_error else "HumanSys"
            back = ("/account/personnel" if self.path in ("/account/connect", "/account/auto-connect")
                    and "form" in locals() and form.get("returnTo") == "/account/personnel" else
                    "/account/personnel" if self.path in ("/account/list", "/account/print-pdf")
                    and "form" in locals() and form.get("category") == "15" else
                    "/research" if research_error else
                    "/account/personnel" if self.path.startswith("/account/personnel/") else
                    "/account" if account_error else "/")
            self._send(page("結果不明", f'<section class="card"><h2>遠端操作結果不明</h2>'
                            f'<p class="warn">{esc(code)}。請先到 {system} 查詢，勿直接重試。</p>'
                            f'<a class="button secondary" href="{back}">返回</a></section>',
                            active="personnel" if back == "/account/personnel" else
                            "research" if research_error else
                            "account" if account_error else "business"), 502)

    def _handle_draft(self, session: LocalSession, form: dict[str, str],
                      upload: tuple[str, bytes] | None) -> None:
        if form.get("operation") != "save" or form.get("confirmDraft") != "yes":
            raise ValueError("請勾選確認只儲存一筆；沒有送出。")
        if upload is not None:
            raise ValueError("請在 proofFile[0].filename 填入本機附件檔名；此頁不接受上傳。")
        filename = form.get("proofFilename", "").strip()
        source = record_from_form(form, [filename])
        prepared = prepare_record(source, Path(__file__).resolve().parent)
        self._send_draft_once(session, prepared)

    def _send_draft_once(self, session: LocalSession, prepared: dict[str, object]) -> None:
        response = open_page(session.opener, DEFAULT_TARGET, DEFAULT_USER_AGENT)
        body = response.read(2_000_000)
        if response.getcode() != 200 or response.geturl() != DEFAULT_TARGET:
            raise ValueError("新增頁不是預期頁面，未儲存。")
        status, final_url = save_draft(session.opener, body, response.geturl(),
                                       prepared, DEFAULT_USER_AGENT)
        safe = urllib.parse.urlsplit(final_url)
        result = f"HTTP {status}，最終路徑 {safe.path}" if safe.hostname == "cis.ncu.edu.tw" else f"HTTP {status}"
        self._send(page("已發出儲存", f'<section class="card"><h2>儲存請求已發出一次</h2>'
                        f'<p>{esc(result)}</p><p class="warn">尚未確認清單是否新增。請到 HumanSys 核對；勿直接重試，以免重複建立。這一步沒有送審。</p>'
                        '<a class="button secondary" href="/">返回</a></section>'))

    def _handle_submit(self, session: LocalSession, form: dict[str, str]) -> None:
        roc_date = validate_roc_date(form.get("date", "").strip())
        application_id = form.get("applicationId", "").strip()
        if not re.fullmatch(r"\d+", application_id):
            raise ValueError("申請單號不正確。")
        rows, _ = fetch_list(session)
        selected = [row for row in rows_for_start_date(rows, roc_date)
                    if row.application_id == application_id]
        if len(selected) != 1:
            raise ValueError("選取的申請不在目前清單中唯一出現；沒有送審。")
        action, body = selected_submit_form(selected[0], roc_date)
        # This is the only remote mutation in this route. Never retry automatically.
        response = open_page(session.opener, action, DEFAULT_USER_AGENT,
                             data=body, referer=LIST_URL)
        response.read(2_000_000)
        try:
            latest, _ = fetch_list(session)
            state = [row.values["表單狀態"] for row in latest
                     if row.application_id == application_id]
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError, OSError):
            state = []
        if state == ["簽核中"]:
            content = '<p class="ok">已送審一次，清單回查狀態為「簽核中」。</p>'
        else:
            content = ('<p class="warn">送審請求已發出，但無法確認為「簽核中」。'
                       '請到 HumanSys 手動核對；勿直接重試。</p>')
        self._send(page("送審結果", f'<section class="card"><h2>申請單 {esc(application_id)}</h2>'
                        f'{content}<a class="button secondary" href="/">返回</a></section>'))


def main() -> None:
    parser = argparse.ArgumentParser(description="只在本機執行的 HumanSys 公差申請網頁。")
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port 必須在 1024–65535 之間。")
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"本機網頁（{APP_VERSION}）：http://127.0.0.1:{args.port}/")
    print("只監聽 127.0.0.1；按 Ctrl+C 停止。公差申請、計劃檢視及主持人費共用 Portal session.json；報帳使用 session01.json。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSIONS.clear()
        RESEARCH_SESSIONS.clear()
        APPROPRIATION_SESSIONS.clear()
        APPROPRIATION_ADD_ATTEMPTS.clear()
        ACCOUNT_SESSIONS.clear()
        ACCOUNT_SAVE_ATTEMPTS.clear()
        server.server_close()


if __name__ == "__main__":
    main()
