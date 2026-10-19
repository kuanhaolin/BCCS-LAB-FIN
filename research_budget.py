"""Read-only ResearchSys project list and guarded budget-history PDF printing."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import urllib.parse
import urllib.request

from humansys_sso_probe import (DEFAULT_USER_AGENT, CookieTracker,
                                find_consent_submission, find_sso_form,
                                open_page, parse_cookie_header, seed_cookie_jar)


ORIGIN = "https://cis.ncu.edu.tw"
LIST_PATH = "/ResearchSys/budget/budgetInfo"
PRINT_PATH = "/ResearchSys/budget/budgetPrint"
LIST_URL = ORIGIN + LIST_PATH
OUTPUT_DIR = Path(__file__).with_name("prints")
RENDERER = Path(__file__).parent / "browser" / "account_render_pdf.mjs"
MAX_BODY = 8_000_000
COOKIE_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


@dataclass(frozen=True)
class Project:
    budget_id: str
    number: str
    period: str
    name: str
    principal: str
    unit: str
    sponsor: str
    start: str
    end: str


def cookies_from_header(raw: str) -> dict[str, str]:
    if len(raw) > 16_000 or "\r" in raw or "\n" in raw:
        raise ValueError("ResearchSys Cookie 格式不正確。")
    values: dict[str, str] = {}
    for part in raw.split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError("ResearchSys Cookie 格式不正確。")
        name, value = (piece.strip() for piece in part.split("=", 1))
        if (not COOKIE_RE.fullmatch(name) or not value or name in values
                or any(char in value for char in "\r\n;")):
            raise ValueError("ResearchSys Cookie 格式不正確或名稱重複。")
        values[name] = value
    if not values.get("laravel_session") or len(values) > 50:
        raise ValueError("ResearchSys Cookie 至少需要 laravel_session。")
    return values


class ResearchRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != "https" or target.hostname != "cis.ncu.edu.tw"
                or not target.path.startswith("/ResearchSys/")):
            raise ValueError("ResearchSys Session 已失效或導向其他系統；沒有列印。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResearchSsoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != "https" or target.hostname not in
                ("portal.ncu.edu.tw", "cis.ncu.edu.tw")
                or (target.hostname == "cis.ncu.edu.tw"
                    and target.path != "/ResearchSys"
                    and not target.path.startswith("/ResearchSys/"))
                or self.hops >= 20):
            raise ValueError("ResearchSys 登入導向非預期位置；沒有列印。")
        self.hops += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def bootstrap_research_from_portal(portal_cookie: str) -> tuple[urllib.request.OpenerDirector, list[Project]]:
    """Use the saved Portal SSO cookies to obtain ResearchSys's own session in memory."""
    try:
        cookies = dict(parse_cookie_header(portal_cookie))
    except ValueError as exc:
        raise ValueError("Portal Session 無法辨識，請重新貼上 Cookie。") from exc
    if not all(cookies.get(name) for name in ("portal", "SESSION", "XSRF-TOKEN")):
        raise ValueError("請先在公差申請頁更新 Portal Session。")
    jar = http.cookiejar.CookieJar()
    seed_cookie_jar(jar, "portal.ncu.edu.tw", list(cookies.items()))
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    sso = urllib.request.build_opener(
        CookieTracker(jar), urllib.request.HTTPSHandler(context=context), ResearchSsoRedirect()
    )
    response = open_page(sso, LIST_URL, DEFAULT_USER_AGENT)
    consent_submitted = False
    for _ in range(8):
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("ResearchSys 登入頁過大；沒有列印。")
        final_url = response.geturl()
        target = urllib.parse.urlsplit(final_url)
        content_type = response.headers.get("Content-Type", "")
        if target.hostname == "portal.ncu.edu.tw" and target.path in ("/expired", "/login"):
            raise ValueError("Portal Session 未登入或已過期；請先在 Portal 重新登入，再貼上最新 Cookie。")
        if response.getcode() == 200 and final_url == LIST_URL:
            try:
                parse_projects(body, content_type)
                break
            except ValueError:
                pass
        bridge = find_sso_form(body, content_type, final_url)
        if bridge:
            method, action, fields = bridge
            parsed = urllib.parse.urlsplit(action)
            if (parsed.scheme != "https" or parsed.hostname not in
                    ("portal.ncu.edu.tw", "cis.ncu.edu.tw")
                    or (parsed.hostname == "cis.ncu.edu.tw"
                        and parsed.path != "/ResearchSys"
                        and not parsed.path.startswith("/ResearchSys/"))):
                raise ValueError("ResearchSys 登入表單導向非預期位置；沒有列印。")
            data = urllib.parse.urlencode(fields).encode("utf-8")
            if method.lower() == "get":
                separator = "&" if urllib.parse.urlsplit(action).query else "?"
                action += separator + data.decode("utf-8")
                data = None
            elif method.lower() != "post":
                raise ValueError("ResearchSys 登入表單方法不支援；沒有列印。")
            response = open_page(sso, action, DEFAULT_USER_AGENT,
                                 data=data,
                                 referer=final_url)
            continue
        consent = (find_consent_submission(body, content_type, final_url)
                   if not consent_submitted else None)
        if consent:
            action, fields = consent
            response = open_page(sso, action, DEFAULT_USER_AGENT,
                                 data=urllib.parse.urlencode(fields).encode("utf-8"),
                                 referer=final_url)
            consent_submitted = True
            continue
        if target.hostname == "cis.ncu.edu.tw" and (
            target.path == "/ResearchSys" or target.path.startswith("/ResearchSys/")
        ):
            response = open_page(sso, LIST_URL, DEFAULT_USER_AGENT)
            continue
        raise ValueError("Portal Session 尚未完成 ResearchSys 登入；請先在公差申請頁更新 Session。")
    else:
        raise ValueError("ResearchSys 單一登入未完成；沒有列印。")
    values = {cookie.name: cookie.value for cookie in jar
              if cookie.domain.lstrip(".") == "cis.ncu.edu.tw"
              and cookie.path.startswith("/ResearchSys") and cookie.secure}
    if not values.get("laravel_session"):
        raise ValueError("Portal 登入後未取得 ResearchSys Session；沒有列印。")
    opener = research_opener(values)
    return opener, fetch_projects(opener)


def research_opener(values: dict[str, str]) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    for name, value in values.items():
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain="cis.ncu.edu.tw", domain_specified=False, domain_initial_dot=False,
            path="/ResearchSys", path_specified=True, secure=True, expires=None,
            discard=True, comment=None, comment_url=None, rest={"HttpOnly": None},
            rfc2109=False,
        ))
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context), ResearchRedirect(),
    )


def _get(opener: urllib.request.OpenerDirector, url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "text/html,application/pdf",
        "Referer": LIST_URL,
    }, method="GET")
    with opener.open(request, timeout=30) as response:
        body = response.read(MAX_BODY + 1)
        actual = urllib.parse.urlsplit(response.geturl())
        expected = urllib.parse.urlsplit(url)
        if (response.status != 200 or actual.scheme != "https"
                or actual.hostname != "cis.ncu.edu.tw"
                or actual.path != expected.path or actual.query != expected.query
                or len(body) > MAX_BODY):
            raise ValueError("ResearchSys 頁面未正常載入；沒有列印。")
        return body, response.headers.get("Content-Type", "")


class ProjectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.table_depth = 0
        self.row_depth = 0
        self.cell_depth = 0
        self.cells: list[str] = []
        self.cell_parts: list[str] = []
        self.budget_id = ""
        self.print_form = False
        self.has_header = False
        self.rows: list[Project] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.depth += 1
            self.table_depth = self.depth
        elif tag == "tr" and self.table_depth:
            self.row_depth = self.table_depth
            self.cells, self.budget_id = [], ""
        elif tag in ("td", "th") and self.row_depth:
            self.cell_depth = self.row_depth
            self.cell_parts = []
        elif tag == "br" and self.cell_depth:
            self.cell_parts.append(" ")
        elif tag == "form" and self.row_depth:
            action = urllib.parse.urljoin(LIST_URL, attributes.get("action") or "")
            self.print_form = action == ORIGIN + PRINT_PATH and (attributes.get("method") or "get").lower() == "get"
        elif tag == "input" and self.print_form and self.row_depth:
            if attributes.get("name") == "BudgetInfoId":
                self.budget_id = attributes.get("value") or ""

    def handle_data(self, data):
        if self.cell_depth:
            self.cell_parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell_depth:
            self.cells.append(" ".join("".join(self.cell_parts).split()))
            self.cell_depth = 0
        elif tag == "form":
            self.print_form = False
        elif tag == "tr" and self.row_depth:
            if self.cells[:3] == ["計畫流水號", "期別", "計畫名稱"]:
                self.has_header = True
            elif self.has_header and len(self.cells) == 9 and re.fullmatch(r"\d{1,20}", self.budget_id):
                self.rows.append(Project(self.budget_id, *self.cells[:8]))
            self.row_depth = 0
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth < self.table_depth:
                self.table_depth = 0


def parse_projects(body: bytes, content_type: str) -> list[Project]:
    if "html" not in content_type.lower():
        raise ValueError("ResearchSys 清單不是 HTML；沒有列印。")
    parser = ProjectParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    if not parser.has_header:
        raise ValueError("ResearchSys 計畫清單未辨識；請更新 Session。")
    ids = [row.budget_id for row in parser.rows]
    if len(ids) != len(set(ids)):
        raise ValueError("計畫清單含重複的 BudgetInfoId；沒有列印。")
    return parser.rows


def fetch_projects(opener: urllib.request.OpenerDirector,
                   is_close: str = "") -> list[Project]:
    if is_close not in ("", "Y", "N"):
        raise ValueError("結案篩選值不正確；沒有查詢。")
    url = LIST_URL + "?IsClose=" + is_close
    return parse_projects(*_get(opener, url))


def _cookies(opener: urllib.request.OpenerDirector) -> list[dict[str, str]]:
    processors = [handler for handler in opener.handlers
                  if isinstance(handler, urllib.request.HTTPCookieProcessor)]
    if len(processors) != 1:
        raise ValueError("無法讀取 ResearchSys Session；沒有列印。")
    return [{"name": cookie.name, "value": cookie.value, "path": cookie.path}
            for cookie in processors[0].cookiejar
            if cookie.domain.lstrip(".") == "cis.ncu.edu.tw"
            and cookie.path.startswith("/ResearchSys") and cookie.secure]


class PrintTargetParser(HTMLParser):
    """Find only an explicit '列印經分表' control on budgetPrint."""

    def __init__(self, budget_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.budget_id = budget_id
        self.targets: list[str] = []
        self.anchor_href = ""
        self.anchor_text: list[str] = []
        self.form_action = ""
        self.form_method = ""
        self.form_fields: dict[str, str] = {}

    def _target(self, raw: str) -> None:
        url = urllib.parse.urljoin(ORIGIN + PRINT_PATH, raw)
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if (parts.scheme != "https" or parts.hostname != "cis.ncu.edu.tw"
                or parts.username or parts.password or parts.fragment
                or parts.path != PRINT_PATH + "/" + self.budget_id
                or query != {"action": ["pdf"]}):
            return
        self.targets.append(url)

    @staticmethod
    def _onclick_url(value: str) -> str:
        match = re.search(r"(?:window\.open|location(?:\.href)?\s*=)\s*\(?\s*['\"]([^'\"]+)['\"]", value)
        return match.group(1) if match else ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            self.anchor_href = attributes.get("href") or ""
            self.anchor_text = []
            if "列印經分表" in (attributes.get("title") or ""):
                onclick_url = self._onclick_url(attributes.get("onclick") or "")
                if onclick_url:
                    self._target(onclick_url)
        elif tag == "form":
            self.form_action = attributes.get("action") or ""
            self.form_method = (attributes.get("method") or "get").lower()
            self.form_fields = {}
        elif tag == "input":
            name = attributes.get("name")
            if self.form_action and name and (attributes.get("type") or "text").lower() == "hidden":
                self.form_fields[name] = attributes.get("value") or ""
            label = attributes.get("value") or ""
            if "列印經分表" in label:
                onclick_url = self._onclick_url(attributes.get("onclick") or "")
                if onclick_url:
                    self._target(onclick_url)
                elif self.form_action and self.form_method == "get":
                    query = urllib.parse.urlencode(self.form_fields)
                    self._target(self.form_action + ("?" + query if query else ""))
        elif tag == "button" and "列印經分表" in (attributes.get("title") or ""):
            onclick_url = self._onclick_url(attributes.get("onclick") or "")
            if onclick_url:
                self._target(onclick_url)

    def handle_data(self, data):
        if self.anchor_href:
            self.anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor_href:
            if "列印經分表" in "".join(self.anchor_text):
                self._target(self.anchor_href)
            self.anchor_href = ""
            self.anchor_text = []
        elif tag == "form":
            self.form_action, self.form_method, self.form_fields = "", "", {}


def budget_print_target(body: bytes, budget_id: str) -> str:
    parser = PrintTargetParser(budget_id)
    parser.feed(body.decode("utf-8", errors="replace"))
    unique = set(parser.targets)
    if len(unique) != 1:
        raise ValueError("官方頁面尚未辨識到唯一的「列印經分表」連結；沒有保存 PDF。")
    return unique.pop()


def save_budget_pdf(opener: urllib.request.OpenerDirector, row: Project,
                    folder: Path = OUTPUT_DIR) -> Path:
    if (not re.fullmatch(r"\d{1,20}", row.budget_id)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,30}", row.number)
            or not re.fullmatch(r"\d{1,3}", row.period)):
        raise ValueError("計畫或期別格式不正確；沒有列印。")
    url = ORIGIN + PRINT_PATH + "?BudgetInfoId=" + row.budget_id
    body, content_type = _get(opener, url)
    if "html" in content_type.lower() and not body.startswith(b"%PDF-"):
        url = budget_print_target(body, row.budget_id)
        body, content_type = _get(opener, url)
    if body.startswith(b"%PDF-"):
        pdf = body
    elif "html" in content_type.lower():
        document = body.decode("utf-8", errors="replace")
        if ("<html" not in document.lower() or "password" in document.lower()
                or "經分表" not in document
                or row.number not in document):
            raise ValueError("官方列印頁內容無法核對計畫；沒有保存 PDF。")
        data = {"mode": "research", "url": url, "number": row.number,
                "budgetId": row.budget_id, "cookies": _cookies(opener)}
        if not data["cookies"]:
            raise ValueError("ResearchSys Session 已失效；沒有列印。")
        try:
            result = subprocess.run(["node", str(RENDERER)],
                                    input=json.dumps(data).encode("utf-8"),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("本機 Chrome 無法完成經分表列印；沒有保存檔案。") from exc
        if result.returncode:
            raise ValueError("官方經分表無法轉成 PDF；沒有保存檔案。")
        pdf = result.stdout
    else:
        raise ValueError("官方列印頁格式不支援；沒有保存 PDF。")
    if not (100 < len(pdf) <= MAX_BODY and pdf.startswith(b"%PDF-")
            and b"%%EOF" in pdf[-1024:]):
        raise ValueError("列印結果不是完整 PDF；沒有保存檔案。")
    if folder.is_symlink():
        raise ValueError("PDF 資料夾不可為符號連結。")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"經分表_{row.number}_第{row.period}期.pdf"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("同名 PDF 已存在；沒有覆蓋，請先檢查資料夾。") from exc
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(pdf)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
