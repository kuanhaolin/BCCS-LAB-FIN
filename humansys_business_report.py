"""Read HumanSys business records and print one verified record to PDF."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import http.cookiejar
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

from humansys_sso_probe import CookieTracker, DEFAULT_USER_AGENT, open_page


REPORT_URL = "https://cis.ncu.edu.tw/HumanSys/personal/personalAttend/personalBusinessRecordReport"
REPORT_PATH = urllib.parse.urlsplit(REPORT_URL).path
OUTPUT_DIR = Path(__file__).with_name("prints")
RENDERER = Path(__file__).parent / "browser" / "account_render_pdf.mjs"
MAX_BODY = 10 * 1024 * 1024
DISPLAY_FIELDS = ("申請單號", "開始日期", "結束日期", "本次申請時數", "表單狀態")


@dataclass(frozen=True)
class BusinessReportRow:
    values: dict[str, str]
    print_url: str | None

    @property
    def number(self) -> str:
        return self.values["申請單號"]


def _print_url(attributes: dict[str, str], number: str) -> str | None:
    candidates = [attributes.get("href", ""), attributes.get("data-url", "")]
    onclick = attributes.get("onclick", "")
    candidates += re.findall(r"['\"]([^'\"]+)['\"]", onclick)
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate == "#" or candidate.lower().startswith("javascript:"):
            continue
        url = urllib.parse.urlsplit(urllib.parse.urljoin(REPORT_URL, candidate))
        if (url.scheme != "https" or url.hostname != "cis.ncu.edu.tw"
                or not url.path.startswith("/HumanSys/") or url.fragment):
            continue
        if not re.search(r"recordreport|print|pdf", url.path, re.I):
            continue
        if re.search(r"update|delete|datatoeform", url.path, re.I):
            continue
        query_values = [value for values in urllib.parse.parse_qs(url.query).values()
                        for value in values]
        if number not in url.path.split("/") and number not in query_values:
            continue
        return urllib.parse.urlunsplit(url)
    return None


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: tuple[str, ...] = ()
        self.matched_header = False
        self.rows: list[BusinessReportRow] = []
        self._cells: list[tuple[str, str, str, list[tuple[str, dict[str, str]]]]] | None = None
        self._cell: dict | None = None
        self._control: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "tr":
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._cell = {"tag": tag, "data_th": attributes.get("data-th", ""),
                          "text": [], "controls": []}
        elif tag in ("a", "button", "input") and self._cell is not None:
            if tag == "input":
                label = attributes.get("value", "") or attributes.get("title", "")
                self._cell["controls"].append((label, attributes))
            else:
                self._control = {"attributes": attributes, "text": []}

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)
        if self._control is not None:
            self._control["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "button") and self._control is not None and self._cell is not None:
            label = " ".join("".join(self._control["text"]).split())
            label += " " + self._control["attributes"].get("title", "")
            self._cell["controls"].append((label, self._control["attributes"]))
            self._control = None
        elif tag in ("td", "th") and self._cell is not None and self._cells is not None:
            cell = self._cell
            self._cells.append((cell["tag"], " ".join("".join(cell["text"]).split()),
                                cell["data_th"], cell["controls"]))
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            cells = self._cells
            self._cells = None
            if cells and all(cell[0] == "th" for cell in cells):
                headings = tuple(cell[1] for cell in cells)
                if all(field in headings for field in DISPLAY_FIELDS):
                    self.headers = headings
                    self.matched_header = True
                return
            values = {cell[2]: cell[1] for cell in cells if cell[2]}
            if (not all(field in values for field in DISPLAY_FIELDS)
                    and self.headers and len(cells) == len(self.headers)):
                values = dict(zip(self.headers, (cell[1] for cell in cells)))
            if not all(field in values for field in DISPLAY_FIELDS):
                return
            number = values["申請單號"].strip()
            if not re.fullmatch(r"\d{1,20}", number):
                raise ValueError("官方公差紀錄的申請單號格式不正確；已停止。")
            links = {_print_url(attrs, number) for cell in cells for label, attrs in cell[3]
                     if re.search(r"列印|print", label, re.I)}
            links.discard(None)
            self.rows.append(BusinessReportRow(values, next(iter(links)) if len(links) == 1 else None))


def parse_report(body: bytes, content_type: str, final_url: str) -> list[BusinessReportRow]:
    url = urllib.parse.urlsplit(final_url)
    if (url.scheme, url.hostname, url.path) != ("https", "cis.ncu.edu.tw", REPORT_PATH):
        raise ValueError("沒有到達官方公差紀錄頁；已停止。")
    if "html" not in content_type.lower() or len(body) > MAX_BODY:
        raise ValueError("官方公差紀錄頁不是可讀的 HTML；已停止。")
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    parser = _ReportParser()
    parser.feed(body.decode(charset, errors="replace"))
    if not parser.matched_header:
        raise ValueError("官方公差紀錄表格欄位未辨識；已停止。")
    numbers = [row.number for row in parser.rows]
    if len(numbers) != len(set(numbers)):
        raise ValueError("官方公差紀錄有重複單號；已停止。")
    return parser.rows


def fetch_report(session) -> list[BusinessReportRow]:
    response = open_page(session.opener, REPORT_URL, DEFAULT_USER_AGENT)
    body = response.read(MAX_BODY + 1)
    if response.getcode() != 200:
        raise ValueError("官方公差紀錄頁沒有正常載入；已停止。")
    return parse_report(body, response.headers.get("Content-Type", ""), response.geturl())


def _human_cookies(session) -> list[dict[str, str]]:
    trackers = [handler for handler in session.opener.handlers if isinstance(handler, CookieTracker)]
    if len(trackers) != 1:
        raise ValueError("無法讀取本次 HumanSys Cookie；沒有產生 PDF。")
    cookies = [{"name": cookie.name, "value": cookie.value, "path": cookie.path}
               for cookie in trackers[0].cookiejar
               if cookie.domain.lstrip(".") == "cis.ncu.edu.tw"
               and cookie.path.startswith("/HumanSys") and cookie.secure]
    if not cookies:
        raise ValueError("沒有有效的 HumanSys Cookie；沒有產生 PDF。")
    return cookies


def save_report_pdf(session, row: BusinessReportRow, folder: Path = OUTPUT_DIR) -> Path:
    if not row.print_url:
        raise ValueError("官方此筆沒有可辨識的列印連結；沒有產生 PDF。")
    url = _print_url({"href": row.print_url}, row.number)
    if url != row.print_url:
        raise ValueError("列印連結與申請單號不一致；沒有產生 PDF。")
    response = open_page(session.opener, url, DEFAULT_USER_AGENT, referer=REPORT_URL)
    body = response.read(MAX_BODY + 1)
    actual = urllib.parse.urlsplit(response.geturl())
    expected = urllib.parse.urlsplit(url)
    if (response.getcode() != 200 or actual.scheme != "https"
            or actual.hostname != "cis.ncu.edu.tw" or actual.path != expected.path
            or actual.query != expected.query):
        raise ValueError("官方列印頁未正常載入；沒有產生 PDF。")
    if len(body) > MAX_BODY or not body:
        raise ValueError("官方列印頁為空或過大；沒有產生 PDF。")
    if body.startswith(b"%PDF-"):
        pdf = body
    elif "html" in response.headers.get("Content-Type", "").lower():
        if not re.search(r"<title\b[^>]*>[^<]*公差申請單",
                         body.decode("utf-8", errors="replace"), re.I):
            raise ValueError("官方列印頁不是公差申請單；沒有保存檔案。")
        data = {"mode": "humansys", "url": url, "number": row.number,
                "cookies": _human_cookies(session)}
        try:
            result = subprocess.run(["node", str(RENDERER)], input=json.dumps(data).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("本機 Chrome 無法完成 PDF 列印；沒有保存檔案。") from exc
        if result.returncode != 0:
            stage = re.search(rb"Unable to render official print page \(([a-z-]+)\)",
                              result.stderr[:200])
            detail = f"（{stage.group(1).decode('ascii')}）" if stage else ""
            raise ValueError(f"官方列印頁無法轉成 PDF{detail}；沒有保存檔案。")
        pdf = result.stdout
    else:
        raise ValueError("官方列印頁格式不支援；沒有產生 PDF。")
    if not (100 < len(pdf) <= MAX_BODY and pdf.startswith(b"%PDF-")
            and b"%%EOF" in pdf[-1024:]):
        raise ValueError("列印結果不是完整 PDF；沒有保存檔案。")
    if folder.is_symlink():
        raise ValueError("PDF 資料夾不可為符號連結。")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"申請單_{row.number}.pdf"
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
