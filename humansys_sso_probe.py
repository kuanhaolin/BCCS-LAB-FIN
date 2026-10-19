#!/usr/bin/env python3
"""Safely test whether a Portal session can bootstrap a HumanSys session.

The supplied cookie is read with getpass, kept only in memory, and never printed.
Only redacted redirect URLs and a coarse success result are shown.
"""

from __future__ import annotations

import argparse
import getpass
import html
from html.parser import HTMLParser
import http.cookiejar
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_TARGET = (
    "https://cis.ncu.edu.tw/HumanSys/personal/"
    "personalAttendApply/businessApply/create"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 10) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Mobile Safari/537.36"
)


def redact_url(url: str) -> str:
    """Keep the route visible while hiding every query-string value."""
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        safe_query = urllib.parse.urlencode([(key, "REDACTED") for key, _ in query])
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, safe_query, "")
        )
    except ValueError:
        return "<invalid URL>"


def parse_cookie_header(raw: str) -> list[tuple[str, str]]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if raw.lower().startswith(("https://", "http://")):
        raise ValueError("你貼入的是網址；這裡只接受 Cookie: 後面的 NAME=value 內容。")
    cookies: list[tuple[str, str]] = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "Cookie 格式應為 NAME=value；多個 Cookie 以分號分隔。"
            )
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("Cookie 名稱不可為空。")
        if not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]+", name):
            raise ValueError(
                "Cookie 名稱格式不正確；請不要加入網址或其他請求標頭。"
            )
        cookies.append((name, value.strip()))
    if not cookies:
        raise ValueError("沒有讀到 Cookie。")
    return cookies


def seed_cookie_jar(
    jar: http.cookiejar.CookieJar,
    cookie_domain: str,
    cookies: list[tuple[str, str]],
) -> None:
    domain = cookie_domain.strip().lstrip(".")
    if not domain or "/" in domain or ":" in domain:
        raise ValueError("--cookie-domain 只填網域，例如 portal.ncu.edu.tw")

    for name, value in cookies:
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain="." + domain,
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
        )


class RedirectTracker(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self.max_redirects = max_redirects
        self.hops: list[tuple[int, str, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if len(self.hops) >= self.max_redirects:
            raise urllib.error.HTTPError(
                req.full_url, code, "重新導向次數過多", headers, fp
            )
        self.hops.append((code, req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CookieTracker(urllib.request.HTTPCookieProcessor):
    def __init__(self, jar: http.cookiejar.CookieJar) -> None:
        super().__init__(jar)
        self.portal_request_cookie_names: set[str] = set()

    def http_request(self, request):  # type: ignore[no-untyped-def]
        request = super().http_request(request)
        if urllib.parse.urlsplit(request.full_url).hostname == "portal.ncu.edu.tw":
            cookie_header = request.get_header("Cookie") or ""
            self.portal_request_cookie_names.update(
                part.split("=", 1)[0].strip()
                for part in cookie_header.split(";")
                if "=" in part
            )
        return request

    https_request = http_request


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None
        self._button: dict[str, str] | None = None
        self._button_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        values = dict(attrs)
        if tag.lower() == "form":
            self._current = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").lower(),
                "fields": [],
                "controls": [],
                "buttons": [],
            }
            self.forms.append(self._current)
        elif tag.lower() == "input" and self._current is not None:
            name = values.get("name")
            if name:
                fields = self._current["fields"]
                assert isinstance(fields, list)
                fields.append((name, values.get("value", "")))
            control_type = values.get("type", "text").lower()
            controls = self._current["controls"]
            assert isinstance(controls, list)
            controls.append({
                "name": name or "",
                "value": values.get("value", "on" if control_type in ("checkbox", "radio") else ""),
                "type": control_type,
                "checked": "checked" in values,
                "disabled": "disabled" in values,
            })
            if control_type == "submit":
                buttons = self._current["buttons"]
                assert isinstance(buttons, list)
                buttons.append({"name": name or "", "value": values.get("value", ""),
                                "label": values.get("value", "")})
        elif tag.lower() == "button" and self._current is not None:
            if values.get("type", "submit").lower() == "submit":
                self._button = {"name": values.get("name", ""),
                                "value": values.get("value", "")}
                self._button_text = []

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "button" and self._button is not None and self._current is not None:
            self._button["label"] = " ".join("".join(self._button_text).split())
            buttons = self._current["buttons"]
            assert isinstance(buttons, list)
            buttons.append(self._button)
            self._button = None
        if tag.lower() == "form":
            self._current = None


class ConsentControlParser(HTMLParser):
    """Read control metadata only; never retain or print form values."""

    def __init__(self) -> None:
        super().__init__()
        self.controls: list[tuple[str, str, str, bool]] = []
        self._inside_target_form = False
        self._inside_button = False
        self._button_text: list[str] = []
        self._button_name = ""
        self._button_type = ""

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        values = dict(attrs)
        if tag.lower() == "form":
            self._inside_target_form = True
        elif self._inside_target_form and tag.lower() == "input":
            control_type = values.get("type", "text").lower()
            self.controls.append(
                (
                    "input",
                    values.get("name", ""),
                    control_type,
                    "checked" in values,
                )
            )
            if control_type in ("submit", "button"):
                label = values.get("value", "").lower()
                if any(word in label for word in ("同意", "授權", "approve", "agree", "allow")):
                    intent = "同意"
                elif any(word in label for word in ("拒絕", "取消", "deny", "reject", "cancel")):
                    intent = "拒絕／取消"
                else:
                    intent = "其他"
                print(
                    f"    按鈕：類型={control_type}、"
                    f"名稱={values.get('name') or '(無)'}、用途={intent}"
                )
        elif self._inside_target_form and tag.lower() == "button":
            self._inside_button = True
            self._button_text = []
            self._button_name = values.get("name", "")
            self._button_type = values.get("type", "submit").lower()

    def handle_data(self, data: str) -> None:
        if self._inside_button:
            self._button_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "button" and self._inside_button:
            label = " ".join("".join(self._button_text).split()).lower()
            if any(word in label for word in ("同意", "授權", "approve", "agree", "allow")):
                intent = "同意"
            elif any(word in label for word in ("拒絕", "取消", "deny", "reject", "cancel")):
                intent = "拒絕／取消"
            else:
                intent = "其他"
            self.controls.append(("button", self._button_name, self._button_type, intent == "同意"))
            print(f"    按鈕：類型={self._button_type}、名稱={self._button_name or '(無)'}、用途={intent}")
            self._inside_button = False
        elif tag.lower() == "form":
            self._inside_target_form = False


class FormStructureParser(HTMLParser):
    """Collect form actions and control names without retaining values."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        values = dict(attrs)
        tag = tag.lower()
        if tag == "form":
            self._current = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").upper(),
                "controls": [],
            }
            self.forms.append(self._current)
        elif self._current is not None and tag in ("input", "select", "textarea"):
            name = values.get("name", "")
            if name:
                controls = self._current["controls"]
                assert isinstance(controls, list)
                controls.append((name, values.get("type", tag), "required" in values))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def find_sso_form(body: bytes, content_type: str, base_url: str):
    if "html" not in content_type.lower():
        return None
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    text = body.decode(charset, errors="replace")
    parser = FormParser()
    parser.feed(text)

    for form in parser.forms:
        fields = form["fields"]
        assert isinstance(fields, list)
        names = {str(name).lower() for name, _ in fields}
        # Common SAML/CAS/OIDC bridge forms. Avoid submitting ordinary app forms.
        looks_like_sso = bool(
            names.intersection(
                {"samlresponse", "relaystate", "ticket", "code", "state", "wa", "wresult"}
            )
        )
        if not looks_like_sso:
            continue
        action = urllib.parse.urljoin(base_url, html.unescape(str(form["action"])))
        return str(form["method"]), action, fields
    return None


def find_consent_submission(body: bytes, content_type: str, base_url: str):
    """Return the browser-default approved fields for the known Portal form."""
    if "html" not in content_type.lower():
        return None
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    parser = FormParser()
    parser.feed(body.decode(charset, errors="replace"))
    for form in parser.forms:
        action = urllib.parse.urljoin(base_url, html.unescape(str(form["action"])))
        parsed = urllib.parse.urlsplit(action)
        if (parsed.scheme, parsed.hostname, parsed.path, str(form["method"])) != (
            "https", "portal.ncu.edu.tw", "/oauth/leaving", "post"
        ):
            continue
        buttons = form["buttons"]
        assert isinstance(buttons, list)
        negative = ("拒絕", "取消", "返回", "deny", "reject", "cancel", "back")
        positive = ("同意", "授權", "確定", "確認", "繼續", "前往", "進入", "approve", "agree", "allow", "authorize", "continue", "confirm")
        acceptable = [
            button for button in buttons
            if not any(word in str(button["label"]).lower() for word in negative)
        ]
        approved = [
            button for button in acceptable
            if any(word in str(button["label"]).lower() for word in positive)
        ]
        if len(approved) == 1:
            button = approved[0]
        elif len(buttons) == 1 and len(acceptable) == 1:
            button = acceptable[0]
        else:
            raise ValueError("無法確定授權表單的同意按鈕；已停止，沒有提交。")
        controls = form["controls"]
        assert isinstance(controls, list)
        fields: list[tuple[str, str]] = []
        for control in controls:
            name = str(control["name"])
            kind = str(control["type"])
            if not name or control["disabled"] or kind in ("submit", "button", "reset", "image", "file"):
                continue
            if kind in ("checkbox", "radio") and not control["checked"]:
                continue
            fields.append((name, str(control["value"])))
        if not any(name == "_csrf" for name, _ in fields):
            raise ValueError("授權表單沒有 _csrf 欄位；已停止，沒有提交。")
        if button["name"]:
            fields.append((str(button["name"]), str(button["value"])))
        return action, fields
    return None


def show_portal_page_diagnostics(body: bytes, content_type: str, base_url: str) -> None:
    """Show structural hints without printing the page or any field values."""
    if "html" not in content_type.lower():
        return
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    page = body.decode(charset, errors="replace")
    lowered = page.lower()
    hints = {
        "登入入口": "login via qr-code" in lowered or "sign in" in lowered,
        "授權同意字樣": any(
            marker in lowered for marker in ("consent", "approve", "同意授權")
        ),
        "密碼欄位": bool(re.search(r"<input[^>]*type=[\"\']password", page, re.I)),
        "自動提交程式": bool(
            re.search(r"\.submit\s*\(|location\.(?:href|replace)", page, re.I)
        ),
    }
    print("  Portal 頁面特徵：" + "、".join(
        f"{name}={'有' if present else '無'}" for name, present in hints.items()
    ))
    forms = FormParser()
    forms.feed(page)
    print(f"  Portal HTML 表單數：{len(forms.forms)}")
    for form in forms.forms[:3]:
        action = urllib.parse.urljoin(base_url, html.unescape(str(form["action"])))
        fields = form["fields"]
        assert isinstance(fields, list)
        names = ", ".join(str(name) for name, _ in fields[:15])
        print(f"    {str(form['method']).upper()} {redact_url(action)}；欄位：{names or '(無)'}")
    if any(
        urllib.parse.urlsplit(
            urllib.parse.urljoin(base_url, str(form["action"]))
        ).path == "/oauth/leaving"
        for form in forms.forms
    ):
        controls = ConsentControlParser()
        controls.feed(page)
        checkboxes = [
            (name, checked)
            for kind, name, control_type, checked in controls.controls
            if kind == "input" and control_type in ("checkbox", "radio")
        ]
        for name, checked in checkboxes[:15]:
            print(f"    勾選欄位：{name or '(無名稱)'}={'已勾' if checked else '未勾'}")


def show_application_form_structure(body: bytes, content_type: str, base_url: str) -> None:
    if "html" not in content_type.lower():
        print("  申請頁不是 HTML，無法列出表單欄位。")
        return
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    parser = FormStructureParser()
    parser.feed(body.decode(charset, errors="replace"))
    print(f"  HumanSys HTML 表單數：{len(parser.forms)}")
    for index, form in enumerate(parser.forms[:5], start=1):
        action = urllib.parse.urljoin(base_url, html.unescape(str(form["action"])))
        controls = form["controls"]
        assert isinstance(controls, list)
        print(f"    表單 {index}：{form['method']} {redact_url(action)}")
        print("    欄位名稱/類型：" + (
            ", ".join(
                f"{name}({kind}{',必填' if required else ''})"
                for name, kind, required in controls[:80]
            ) or "(無)"
        ))


def open_page(opener, url: str, user_agent: str, data: bytes | None = None,
              referer: str | None = None):
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        parts = urllib.parse.urlsplit(url)
        headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    return opener.open(request, timeout=30)


def show_new_hops(tracker: RedirectTracker, start: int) -> int:
    for code, source, destination in tracker.hops[start:]:
        print(f"  {code}  {redact_url(source)}")
        print(f"       -> {redact_url(destination)}")
    return len(tracker.hops)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="測試 Portal Session 能否經 SSO 自動取得 HumanSys Session。"
    )
    parser.add_argument(
        "--entry-url",
        default=DEFAULT_TARGET,
        help="從 Portal 點 HumanSys 時的入口網址；未知時可先用預設 HumanSys 網址。",
    )
    parser.add_argument(
        "--target-url",
        default=DEFAULT_TARGET,
        help="登入後要驗證的 HumanSys 頁面。",
    )
    parser.add_argument(
        "--cookie-domain",
        default="portal.ncu.edu.tw",
        help="這組 Portal Cookie 所屬網域（預設：portal.ncu.edu.tw）。",
    )
    parser.add_argument("--max-redirects", type=int, default=20)
    parser.add_argument(
        "--approve-consent",
        action="store_true",
        help="依 Portal 授權頁目前的勾選狀態，提交同意表單以繼續登入 HumanSys。",
    )
    parser.add_argument(
        "--inspect-application",
        action="store_true",
        help="成功登入後，只列出 HumanSys 表單欄位名稱與提交網址，不送出申請。",
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        default=Path(__file__).with_name("input.json"),
        help="公差申請 JSON 路徑；預設為本程式旁的 input.json。",
    )
    parser.add_argument(
        "--save-draft",
        action="store_true",
        help="實驗性功能：依 JSON 實際儲存一筆公差申請，不送審；須再次輸入 SAVE 確認。",
    )
    parser.add_argument(
        "--inspect-applicant",
        action="store_true",
        help="只讀取並比對填單人識別欄位；只輸出相同／不同，不送出申請。",
    )
    parser.add_argument(
        "--inspect-draft",
        action="store_true",
        help="只讀取申請人 API 並組好儲存請求，但不發送 POST。",
    )
    list_group = parser.add_mutually_exclusive_group()
    list_group.add_argument(
        "--list-date",
        metavar="民國起日",
        help="只讀列出清單頁中此起日的公差申請，例如 1151027。",
    )
    list_group.add_argument(
        "--choose-submit",
        metavar="民國起日",
        help="列出此起日的申請，選擇申請單號並再次確認後送審一次。",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="瀏覽器送出的 User-Agent；預設為本次提供的 Android Chrome。",
    )
    args = parser.parse_args()

    if args.save_draft and (args.inspect_applicant or args.inspect_draft):
        parser.error("--save-draft 不可與只讀檢查選項同時使用。")
    list_date = args.list_date or args.choose_submit
    if list_date:
        from humansys_business_list import LIST_URL, validate_roc_date
        if (args.save_draft or args.inspect_draft or args.inspect_applicant
                or args.inspect_application or args.target_url != DEFAULT_TARGET):
            parser.error("清單查詢／送審不可與其他操作模式或 --target-url 合用。")
        try:
            validate_roc_date(list_date)
        except ValueError as exc:
            parser.error(str(exc))
        target_url = LIST_URL
    else:
        target_url = args.target_url

    prepared = None
    if args.save_draft or args.inspect_draft:
        if args.inspect_application:
            parser.error("儲存／儲存前檢查不可與 --inspect-application 同時使用。")
        from humansys_apply_preview import prepare
        try:
            prepared = prepare(args.input_json)
        except ValueError as exc:
            parser.error(str(exc))
    if args.save_draft:
        print("警告：這會向 HumanSys 傳送一筆真實公差申請的儲存請求。")
        print("目前只支援同一天 08:00–12:00 的教師、同填單人、假日選 N；不送審。")
        print("實際儲存 Payload 尚未與瀏覽器逐欄比對；可能建立紀錄，也可能遭網站退回。")
        print("請先確認清單中沒有相同申請，且不要在失敗時自動重試。")
        print(f"預覽：{prepared['start_date']} {prepared['start_time']}–{prepared['end_time']}、"
              f"縣市代碼 {prepared['zip_code']}、附件 {len(prepared['attachments'])} 件。")
        if input("確認只儲存一次，請輸入 SAVE：").strip() != "SAVE":
            print("已取消，沒有連線或送出。")
            return 2

    if args.approve_consent:
        print("已啟用授權同意：將依 Portal 頁面目前勾選的資料項目提交給 HumanSys。")
    print("請貼 Portal 的 Cookie header（輸入不會顯示，也不會寫入檔案）。")
    print("格式：NAME=value; OTHER_NAME=other_value")
    raw_cookie = getpass.getpass("Portal Cookie: ")

    try:
        cookies = parse_cookie_header(raw_cookie)
        raw_cookie = ""  # Drop the original combined string as soon as possible.
        jar = http.cookiejar.CookieJar()
        seed_cookie_jar(jar, args.cookie_domain, cookies)
        cookies.clear()
    except ValueError as exc:
        print(f"輸入錯誤：{exc}", file=sys.stderr)
        return 2

    tracker = RedirectTracker(args.max_redirects)
    # Python 3.14 enables VERIFY_X509_STRICT by default. Some otherwise
    # trusted legacy certificate chains omit a Subject Key Identifier.
    # Keep CA and hostname verification, but allow that legacy omission.
    tls_context = ssl.create_default_context()
    tls_context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    cookie_tracker = CookieTracker(jar)
    opener = urllib.request.build_opener(
        cookie_tracker,
        urllib.request.HTTPSHandler(context=tls_context),
        tracker,
    )
    user_agent = args.user_agent

    print("\n[1/2] 嘗試 SSO 跳轉")
    printed_hops = 0
    current_url = args.entry_url
    try:
        response = open_page(opener, current_url, user_agent)
        printed_hops = show_new_hops(tracker, printed_hops)

        # Some SSO systems return an HTML form that a browser auto-submits.
        consent_submitted = False
        for _ in range(5):
            body = response.read(2_000_000)
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            sso_form = find_sso_form(body, content_type, final_url)
            if sso_form:
                method, action, fields = sso_form
                print(f"  偵測到 SSO HTML 表單：{method.upper()} {redact_url(action)}")
                encoded = urllib.parse.urlencode(fields).encode("utf-8")
                response = open_page(
                    opener, action, user_agent,
                    data=encoded if method == "post" else None,
                    referer=final_url,
                )
                printed_hops = show_new_hops(tracker, printed_hops)
                continue
            if args.approve_consent and not consent_submitted:
                consent = find_consent_submission(body, content_type, final_url)
                if consent:
                    action, fields = consent
                    print(f"  提交 Portal 授權表單：POST {redact_url(action)}")
                    response = open_page(
                        opener, action, user_agent,
                        data=urllib.parse.urlencode(fields).encode("utf-8"),
                        referer=final_url,
                    )
                    consent_submitted = True
                    printed_hops = show_new_hops(tracker, printed_hops)
                    continue
            break
    except urllib.error.HTTPError as exc:
        printed_hops = show_new_hops(tracker, printed_hops)
        print(f"SSO HTTP 錯誤：{exc.code} {exc.reason}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"連線失敗：{exc.reason}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"授權表單停止：{exc}", file=sys.stderr)
        return 1

    print("\n[2/2] 驗證 HumanSys 公差申請清單" if list_date else
          "\n[2/2] 驗證 HumanSys 公差申請頁")
    try:
        response = open_page(opener, target_url, user_agent)
        printed_hops = show_new_hops(tracker, printed_hops)
        body = response.read(2_000_000)
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        status = response.getcode()
    except urllib.error.HTTPError as exc:
        printed_hops = show_new_hops(tracker, printed_hops)
        print(f"結果：失敗（HTTP {exc.code}）")
        return 1
    except urllib.error.URLError as exc:
        print(f"結果：連線失敗（{exc.reason}）")
        return 1

    text = body.decode("utf-8", errors="replace").lower()
    parsed_final = urllib.parse.urlsplit(final_url)
    reached_humansys = (
        status == 200
        and parsed_final.hostname == "cis.ncu.edu.tw"
        and parsed_final.path.startswith("/HumanSys/")
    )
    application_signals = any(
        signal in text
        for signal in ("businessapply", "personalattendapply", "_token")
    )
    login_signals = any(
        signal in text
        for signal in ("type=\"password\"", "name=\"password\"", "samlrequest")
    )

    print(f"  HTTP：{status}")
    print(f"  最終網址：{redact_url(final_url)}")
    print(f"  Content-Type：{content_type or '(未提供)'}")
    print(f"  本次 Cookie jar 中的 Cookie 數：{sum(1 for _ in jar)}")
    sent_names = ", ".join(sorted(cookie_tracker.portal_request_cookie_names))
    print(f"  送到 Portal 的 Cookie 名稱：{sent_names or '(沒有)'}")
    if parsed_final.hostname == "portal.ncu.edu.tw":
        show_portal_page_diagnostics(body, content_type, final_url)

    if reached_humansys and (application_signals or bool(list_date)) and not login_signals:
        print("結果：成功，Portal Session 可以取得 HumanSys 頁面。")
        if list_date:
            from humansys_business_list import (
                FIELDS, LIST_URL, list_html_diagnostics, parse_list,
                rows_for_start_date, selected_submit_form,
            )
            try:
                all_rows = parse_list(body, content_type, final_url)
            except ValueError as exc:
                print(f"清單解析失敗：{exc}", file=sys.stderr)
                return 1
            matched = rows_for_start_date(all_rows, list_date)
            print(f"目前載入清單中的紀錄：{len(all_rows)} 筆；起日 {list_date}：{len(matched)} 筆")
            if not all_rows:
                print("清單頁原始 HTML 診斷（不顯示個資）：")
                for name, value in list_html_diagnostics(body).items():
                    print(f"  {name}: {value}")
            for row in matched:
                print("  ---")
                for field in FIELDS:
                    print(f"  {field}：{row.values[field] or '(空白)'}")
            if not args.choose_submit:
                print("只讀查詢完成；沒有送審。若清單有分頁，請注意這只包含目前載入的頁面。")
                return 0
            if not matched:
                print("沒有可選的紀錄；沒有送審。若清單有分頁，請先在網站找出該筆。")
                return 1
            application_id = input("請輸入要送審的申請單號（直接 Enter 取消）：").strip()
            candidates = [row for row in matched if row.application_id == application_id]
            if len(candidates) != 1:
                print("申請單號未在此次查詢結果中唯一出現；沒有送審。")
                return 2
            row = candidates[0]
            try:
                action, post_body = selected_submit_form(row, list_date)
            except ValueError as exc:
                print(f"送審前檢查失敗：{exc}", file=sys.stderr)
                return 1
            print(f"即將送審申請單號 {application_id}，起日 {list_date}；送出後不能直接修改。")
            if input(f"確認只送一次，請輸入 SUBMIT {application_id}：").strip() != f"SUBMIT {application_id}":
                print("已取消，沒有送審。")
                return 2
            try:
                response = open_page(opener, action, user_agent, data=post_body, referer=LIST_URL)
                show_new_hops(tracker, printed_hops)
                print(f"送審請求後 HTTP：{response.getcode()}")
                print(f"送審請求後最終網址：{redact_url(response.geturl())}")
                response.read(2_000_000)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                print(f"送審請求結果不明：{exc}", file=sys.stderr)
                print("可能已送出；請先到 HumanSys 清單確認，勿直接重試。")
                return 1
            try:
                verify = open_page(opener, LIST_URL, user_agent)
                verify_body = verify.read(2_000_000)
                verify_rows = parse_list(
                    verify_body, verify.headers.get("Content-Type", ""), verify.geturl()
                )
                exact = [r for r in verify_rows if r.application_id == application_id]
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                print(f"送審已發出，但自動回查失敗：{exc}")
                print("請到 HumanSys 清單手動確認狀態；勿直接重試。")
                return 3
            if len(exact) == 1:
                print(f"回查表單狀態：{exact[0].values['表單狀態']}")
                if exact[0].values["表單狀態"] == "簽核中":
                    print("結果：已送審，並在清單中確認為「簽核中」。")
                    return 0
            print("送審已發出，但尚未確認進入簽核中；請手動查清單，勿直接重試。")
            return 3
        if args.inspect_application:
            show_application_form_structure(body, content_type, final_url)
        if args.inspect_applicant:
            from humansys_business_draft import inspect_applicant
            try:
                diagnosis = inspect_applicant(opener, body, final_url, user_agent)
            except (ValueError, OSError, urllib.error.URLError) as exc:
                print(f"只讀身分檢查失敗：{exc}", file=sys.stderr)
                return 1
            print("只讀身分欄位比對（不顯示編碼）：")
            for name, result in diagnosis.items():
                print(f"  {name}: {result}")
        if args.inspect_draft:
            from humansys_business_draft import build_draft_request
            try:
                assert prepared is not None
                request = build_draft_request(opener, body, final_url, prepared, user_agent)
            except (ValueError, OSError, urllib.error.URLError) as exc:
                print(f"儲存前檢查失敗：{exc}", file=sys.stderr)
                return 1
            print("儲存前檢查通過：")
            print(f"  目標：{request.get_method()} {redact_url(request.full_url)}")
            print(f"  請求資料大小：{len(request.data or b'')} bytes")
            print("  尚未發出儲存 POST，也未建立申請。")
            return 0
        if args.save_draft:
            from humansys_business_draft import save_draft
            try:
                assert prepared is not None
                save_status, save_url = save_draft(
                    opener, body, final_url, prepared, user_agent
                )
            except (ValueError, OSError, urllib.error.URLError) as exc:
                print(f"儲存前檢查或連線失敗：{exc}", file=sys.stderr)
                print("請檢查 HumanSys 清單，確認是否已有紀錄；不要直接重試。")
                return 1
            printed_hops = show_new_hops(tracker, printed_hops)
            print(f"儲存請求後 HTTP：{save_status}")
            print(f"儲存請求後最終網址：{redact_url(save_url)}")
            print("儲存請求只傳送一次；尚未驗證清單是否新增紀錄，也沒有送審。")
            print("請先到 HumanSys 清單確認，勿直接重試以免重複申請。")
            return 3
        return 0
    if reached_humansys and not login_signals:
        print("結果：可能成功；已到 HumanSys，但頁面特徵不足，請檢查跳轉輸出。")
        return 3
    if parsed_final.hostname == "portal.ncu.edu.tw" and all(
        marker in text for marker in ("login via qr-code", "sign in")
    ):
        print("結果：停在 Portal 登入頁；這次的 Cookie 未完成 Portal 授權。")
        print("請比對瀏覽器成功跳入 HumanSys 時，OAuth 授權請求的 Cookie 與狀態碼。")
        return 1
    print("結果：未成功，仍停在登入／SSO 流程或被網站阻擋。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
