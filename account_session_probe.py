#!/usr/bin/env python3
"""Guarded session and form probes for NCU's accounting site.

Cookie values are entered invisibly, kept only in memory, and never printed.
The payee probe uses the site's lookup POST but never its save endpoint.
"""

from __future__ import annotations

import argparse
from datetime import date
import getpass
import hashlib
import http.cookiejar
from html.parser import HTMLParser
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


TARGET = (
    "https://account.ncu.edu.tw/APSWIS_Q/Dept_Add_Q/"
    "DA_SerBug_Menu_Q.asp?Firstload=Y"
)
MAIN_FRAME_TARGET = "https://account.ncu.edu.tw/APSWIS_Q/ACCPR_Main_Q.asp"
HOST = "account.ncu.edu.tw"
COOKIE_NAME_PATTERN = re.compile(r"ASPSESSIONID[A-Z0-9]{8}\Z")
MAX_BODY_BYTES = 1_000_000
ACCOUNT_FRAME_PATHS = {
    "APPY": "/APSWIS_Q/Dept_Add_Q/STD_APPY_Q.asp",
    "APPP": "/APSWIS_Q/Dept_Add_Q/STD_APPP_Q.asp",
    "APPA": "/APSWIS_Q/Dept_Add_Q/STD_APPA_Q.asp",
    "PS": "/APSWIS_Q/Dept_Add_Q/PR_SAVE_Q.asp",
}
ACCOUNT_FRAME_FIELDS = {
    "APPY": ("BUGETNO_1", "BUGCODE_1", "D_AMOUNT_1", "APPYSET", "APYUSERNO"),
    "APPP": ("PRODUCT_1", "AMOUNT_1"),
    "APPA": ("PROX_1", "VENDORID_S_1", "VENNAME_1", "AMOUNT_1"),
    "PS": ("D_STR", "A_STR", "P_STR", "APPYSET"),
}


def appy_frame_url(roc_year: int | None = None) -> str:
    """Build the APPY frame GET observed after choosing petty-cash reimbursement.

    The legacy page expects Big5-encoded query text. This only reads the frame;
    it does not submit the purchase request.
    """
    year = date.today().year - 1911 if roc_year is None else roc_year
    if not 1 <= year <= 999:
        raise ValueError("民國年份不正確。")
    params = [
        ("COPY", ""), ("APPYYEAR", str(year)), ("ISNEW", "Y"),
        ("APYNO", ""), ("APYADD", ""), ("APPYSET", "14"),
        ("MOVETYPE", "6"), ("ISAPPY", "True"), ("ISVOUC", "True"),
        ("ISTEMP", "False"), ("ISCASH", "True"), ("ISPURC", "False"),
        ("SETNAME", "零用金先墊付報支（含國內旅費）" + " " * 10),
        ("APPU", ""), ("DECI", ""), ("SHOWVEN", "True"),
        ("SHOWPRO", "True"), ("APPYBUY", ""), ("VOUSERNO", ""),
    ]
    query = urllib.parse.urlencode(
        params, encoding="cp950", quote_via=urllib.parse.quote,
    )
    return "https://" + HOST + ACCOUNT_FRAME_PATHS["APPY"] + "?" + query


class OffsiteRedirect(Exception):
    """Do not follow an accounting-page redirect to another host."""


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname != HOST:
            raise OffsiteRedirect(parts.hostname or "未知網域")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ProbeResult:
    status: int | None
    path: str
    password_field: bool
    body_digest: str
    offsite_host: str = ""


@dataclass(frozen=True)
class FrameInspection:
    frame: str
    status: int
    correct_path: bool
    is_form: bool
    missing_fields: tuple[str, ...]
    correct_save_action: bool


@dataclass(frozen=True)
class PayeeLookupInspection:
    appy_status: int
    lookup_status: int
    identity_matches: bool
    payee_name_present: bool
    assignment_fields: tuple[str, ...]


class FormNames(HTMLParser):
    """Collect only form names and actions; never retain field values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, set[str]]] = []
        self.current_form: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "form":
            self.forms.append((attrs_dict.get("action") or "", set()))
            self.current_form = len(self.forms) - 1
        elif tag in ("input", "select", "textarea") and self.current_form is not None:
            name = attrs_dict.get("name")
            if name:
                self.forms[self.current_form][1].add(name)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current_form = None


class BudgetOptions(HTMLParser):
    """Count selectable plans in the official first budget selector."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_budget_select = False
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select" and attributes.get("name", "").upper() == "BUGETNO_1":
            self.in_budget_select = True
        elif tag == "option" and self.in_budget_select and attributes.get("value"):
            self.count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.in_budget_select = False


class InputValues(HTMLParser):
    """Capture only the three required APPY inputs; never display values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        name = attributes.get("name")
        if name in {"APPYSET", "NO_MATH", "APYUSERNO"}:
            self.values.setdefault(name, []).append(attributes.get("value") or "")


def inspect_payee_lookup(values: dict[str, str], context: ssl.SSLContext) -> PayeeLookupInspection:
    """Check the advance-payee lookup; POSTs only to CHK_VEN_Q.asp, never save.

    The employee identifier and lookup result remain in local memory and are
    neither returned nor logged. Refuse to look up an ambiguous APPY form.
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(values)),
        urllib.request.HTTPSHandler(context=context),
        SameHostRedirect(),
    )
    appy_url = appy_frame_url()
    appy_request = urllib.request.Request(
        appy_url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml",
                 "Referer": TARGET},
        method="GET",
    )
    with opener.open(appy_request, timeout=20) as response:
        appy_status = response.status
        if appy_status != 200 or urllib.parse.urlsplit(response.geturl()).path != ACCOUNT_FRAME_PATHS["APPY"]:
            raise ValueError("無法讀取經費頁框；沒有查詢受款人。")
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("經費頁框內容過大；沒有查詢受款人。")
    inputs = InputValues()
    inputs.feed(raw.decode("cp950", errors="replace"))
    def exactly_one(name: str) -> str:
        items = inputs.values.get(name, [])
        if len(items) != 1:
            raise ValueError("經費頁框缺少必要身分或類別欄位；沒有查詢受款人。")
        return items[0]
    if exactly_one("APPYSET") != "14" or exactly_one("NO_MATH") != "N":
        raise ValueError("不是已核對的零用金先墊付報支表單；沒有查詢受款人。")
    applicant_id = exactly_one("APYUSERNO")
    if not re.fullmatch(r"[A-Za-z0-9]{5,20}", applicant_id):
        raise ValueError("申請人識別欄位格式不正確；沒有查詢受款人。")

    lookup_url = "https://" + HOST + "/APSWIS_Q/Dept_Add_Q/CHK_VEN_Q.asp"
    body = urllib.parse.urlencode({
        "R_Sign": "Y", "VENDORID": applicant_id,
        "RE_HAND": "parent.MAIN.APPA.FORM1", "I_J": "_1", "PAYKIND": "2",
    }).encode("ascii")
    request = urllib.request.Request(
        lookup_url, data=body,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml",
                 "Content-Type": "application/x-www-form-urlencoded", "Referer": lookup_url,
                 "Origin": "https://" + HOST},
        method="POST",
    )
    with opener.open(request, timeout=20) as response:
        lookup_status = response.status
        if lookup_status != 200 or urllib.parse.urlsplit(response.geturl()).path != "/APSWIS_Q/Dept_Add_Q/CHK_VEN_Q.asp":
            raise ValueError("受款人查詢沒有回到預期頁面；沒有存入。")
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("受款人查詢內容過大；沒有存入。")
    content = raw.decode("cp950", errors="replace")
    identity = re.search(
        r'parent\.MAIN\.APPA\.FORM1\.VENDORID_1\.value\s*=\s*CharToAsc\("([A-Za-z0-9]{5,20})"\)',
        content,
    )
    name = re.search(
        r'parent\.MAIN\.APPA\.FORM1\.VENNAME_1\.value\s*=\s*"([^"\r\n]*)"',
        content,
    )
    fields = tuple(field for field in (
        "VENDORID_1", "VENNAME_1", "BANKNO_1", "ACCOUNT_1", "ACCOUNTNAM_1",
    ) if re.search(r"parent\.MAIN\.APPA\.FORM1\." + field + r"\.value\s*=", content))
    return PayeeLookupInspection(
        appy_status, lookup_status,
        bool(identity and identity.group(1) == applicant_id),
        bool(name and name.group(1).strip()), fields,
    )


def inspect_account_frames(values: dict[str, str], context: ssl.SSLContext) -> list[FrameInspection]:
    """Read four expected frames with the same cookie jar; never POST."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(values)),
        urllib.request.HTTPSHandler(context=context),
        SameHostRedirect(),
    )
    results = []
    for frame, expected_path in ACCOUNT_FRAME_PATHS.items():
        target = (appy_frame_url() if frame == "APPY" else
                  urllib.parse.urljoin("https://" + HOST, expected_path))
        request = urllib.request.Request(
            target,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml",
                     "Referer": TARGET},
            method="GET",
        )
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError("報帳頁框內容過大，已停止只讀檢查。")
            correct_path = urllib.parse.urlsplit(response.geturl()).path == expected_path
            parser = FormNames()
            parser.feed(raw.decode("cp950", errors="replace"))
            required = set(ACCOUNT_FRAME_FIELDS[frame])
            action, names = max(parser.forms,
                                key=lambda entry: len(required.intersection(entry[1])),
                                default=("", set()))
            save_action = (
                urllib.parse.urlsplit(urllib.parse.urljoin(target, action)).path ==
                "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp"
            ) if frame == "PS" else True
            results.append(FrameInspection(
                frame, response.status, correct_path, required.issubset(names),
                tuple(sorted(required - names)), save_action,
            ))
    return results


def cookie_value(raw: str, name: str) -> str:
    value = raw.strip()
    if value.startswith(name + "="):
        value = value[len(name) + 1:]
    if not value or len(value) > 8192 or any(char in value for char in ";\r\n"):
        raise ValueError(f"{name} 的值格式不正確。")
    return value


def cookies_from_header(raw: str) -> dict[str, str]:
    header = raw.strip()
    if header.lower().startswith("cookie:"):
        header = header.split(":", 1)[1].strip()
    if not header or len(header) > 20_000 or any(char in header for char in "\r\n"):
        raise ValueError("Cookie 格式不正確；請貼 key=value; key=value。")
    values: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part:
            continue
        name, separator, value = part.partition("=")
        name = name.strip()
        if not separator or not COOKIE_NAME_PATTERN.fullmatch(name) or name in values:
            raise ValueError("只接受不重複的 ASPSESSIONID Cookie（名稱後接 8 碼英數字）。")
        values[name] = cookie_value(value, name)
    if not values:
        raise ValueError("請至少提供一組 ASPSESSIONID Cookie。")
    return values


def cookie_jar(values: dict[str, str]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for name, value in values.items():
        if not COOKIE_NAME_PATTERN.fullmatch(name):
            raise ValueError("報帳 Cookie 名稱格式不正確。")
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=HOST, domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None,
            discard=True, comment=None, comment_url=None,
            rest={"HttpOnly": None}, rfc2109=False,
        ))
    return jar


def budget_option_count(values: dict[str, str], context: ssl.SSLContext) -> int:
    """Read the budget form and count selectable plans; never submit a form."""
    target = appy_frame_url()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(values)),
        urllib.request.HTTPSHandler(context=context),
        SameHostRedirect(),
    )
    request = urllib.request.Request(
        target,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml",
                 "Referer": TARGET},
        method="GET",
    )
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if (response.status != 200 or
                urllib.parse.urlsplit(response.geturl()).path != ACCOUNT_FRAME_PATHS["APPY"]):
            return 0
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("報帳經費頁內容過大，無法驗證 Session。")
    parser = BudgetOptions()
    parser.feed(raw.decode("cp950", errors="replace"))
    return parser.count


def probe(
    values: dict[str, str], context: ssl.SSLContext, target: str = TARGET,
) -> ProbeResult:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(values)),
        urllib.request.HTTPSHandler(context=context),
        SameHostRedirect(),
    )
    request = urllib.request.Request(
        target,
        headers={"User-Agent": "Mozilla/5.0 (compatible; NCU-account-session-probe/1.0)",
                 "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    try:
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read(MAX_BODY_BYTES)
            path = urllib.parse.urlsplit(response.geturl()).path
            password_field = bool(re.search(
                rb"<input\b[^>]*\btype\s*=\s*['\"]?password\b", data, re.I
            ))
            return ProbeResult(response.status, path, password_field,
                               hashlib.sha256(data).hexdigest())
    except OffsiteRedirect as error:
        return ProbeResult(None, "", False, "", str(error))


def assessment(
    result: ProbeResult, anonymous: ProbeResult, target: str = TARGET,
) -> str:
    if result.offsite_host:
        return f"跳轉至 {result.offsite_host}；尚未確認進入報帳頁。"
    if result.status in (401, 403) or result.password_field or re.search(
        r"/(login|signin|logon)(/|\.|$)", result.path, re.I
    ):
        return "看起來仍在登入／拒絕存取頁。"
    if result.status != 200:
        return "沒有取得 HTTP 200；尚未確認進入報帳頁。"
    if result.path != urllib.parse.urlsplit(target).path:
        return "最終頁面不是目標報帳頁；需要人工核對。"
    if (anonymous.status == result.status and anonymous.path == result.path
            and anonymous.body_digest == result.body_digest):
        return "與未帶 Cookie 的回應完全相同；不能證明 Session 有效。"
    return "內容與未帶 Cookie 不同；可能有 Session 效果，但仍需核對是否真的進入表單。"


def main() -> None:
    parser = argparse.ArgumentParser(description="唯讀測試 ASPSESSIONID Cookie 是否可讀取報帳頁。")
    parser.add_argument(
        "--compat-tls", action="store_true",
        help="僅在憑證因 Missing Subject Key Identifier 失敗時使用；仍會驗證憑證鏈。",
    )
    args = parser.parse_args()
    context = ssl.create_default_context()
    if args.compat_tls:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    print("請貼上目前瀏覽器中的所有 ASPSESSIONID Cookie（key=value; key=value）。")
    print("輸入不會顯示，也不會存檔。")
    try:
        values = cookies_from_header(getpass.getpass("Cookie: "))
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，沒有發出請求。")
        return
    except ValueError as error:
        parser.error(str(error))

    cases = [("未帶 Cookie", {})]
    cases.extend((name, {name: value}) for name, value in values.items())
    cases.append(("輸入的全部 Cookie", values))
    for page_label, target in (("外層選單", TARGET), ("MAIN 內層頁面", MAIN_FRAME_TARGET)):
        print(f"\n{page_label}：")
        results: list[tuple[str, ProbeResult]] = []
        for label, cookies in cases:
            try:
                result = probe(cookies, context, target)
            except ssl.SSLCertVerificationError:
                print("TLS 憑證驗證失敗。若訊息是 Missing Subject Key Identifier，可加 --compat-tls 重試。")
                return
            except (urllib.error.URLError, OSError, TimeoutError):
                print("連線或讀取失敗；沒有得到可比較的結果。")
                return
            results.append((label, result))

        baseline = results[0][1]
        for label, result in results:
            print(f"  {label}：HTTP {result.status if result.status is not None else '未到達目標站'}"
                  f"；路徑 {result.path or '—'}")
            if label != "未帶 Cookie":
                print(f"    判斷：{assessment(result, baseline, target)}")
    print("\n僅發送 GET；沒有送出或修改報帳資料。CookieKey 未納入此次測試。")


if __name__ == "__main__":
    main()
