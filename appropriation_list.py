"""Read the official Appropriation list and save one verified PDF locally."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import urllib.parse

from humansys_sso_probe import DEFAULT_USER_AGENT, open_page


ORIGIN = "https://cis.ncu.edu.tw"
LIST_PATH = "/Appropriation/user/payloadlist_Q.do"
DETAIL_PATH = "/Appropriation/user/payloadlist_detail.do"
PRINT_PATH = "/Appropriation/user/payloadlist_P.do"
LIST_URL = ORIGIN + LIST_PATH + "?isShowAll=true"
OUTPUT_DIR = Path(__file__).with_name("prints")
MAX_BODY = 10 * 1024 * 1024
HEADINGS = ("序號", "預計撥款日期", "製表日期", "計畫流水號", "備註",
            "刪除", "修改", "以舊清冊新增", "詳細內容")
NUMBER_RE = re.compile(r"[0-9]{3}p[0-9]{6}\Z", re.I)


@dataclass(frozen=True)
class AppropriationRow:
    number: str
    payment_date: str
    created_date: str
    plan_number: str
    remarks: str
    detail_url: str


def _official_url(candidate: str, base: str, path: str, number: str) -> str | None:
    url = urllib.parse.urlsplit(urllib.parse.urljoin(base, candidate))
    params = urllib.parse.parse_qs(url.query)
    if (url.scheme != "https" or url.netloc != "cis.ncu.edu.tw"
            or url.path != path or url.fragment
            or set(params) != {"serial_no"}
            or params["serial_no"] != [number]):
        return None
    return urllib.parse.urlunsplit(url)


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings_found = False
        self.rows: list[AppropriationRow] = []
        self.pages: set[int] = {1}
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"tag": tag, "text": [], "links": []}
            self._row.append(self._cell)
        elif tag == "a":
            href = values.get("href") or ""
            url = urllib.parse.urlsplit(urllib.parse.urljoin(LIST_URL, href))
            params = urllib.parse.parse_qs(url.query)
            if (url.scheme == "https" and url.netloc == "cis.ncu.edu.tw"
                    and url.path == LIST_PATH and params.get("isShowAll") == ["true"]
                    and len(params.get("page", [])) == 1
                    and re.fullmatch(r"[0-9]{1,4}", params["page"][0])):
                self.pages.add(int(params["page"][0]))
            if self._cell is not None:
                self._cell["links"].append(href)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr" and self._row is not None:
            cells = self._row
            self._row = None
            texts = [" ".join("".join(cell["text"]).split()) for cell in cells]
            if tuple(texts) == HEADINGS and all(cell["tag"] == "th" for cell in cells):
                self.headings_found = True
                return
            if len(cells) != len(HEADINGS) or not self.headings_found:
                return
            number = texts[0]
            if not NUMBER_RE.fullmatch(number):
                return
            links = {_official_url(link, LIST_URL, DETAIL_PATH, number)
                     for link in cells[-1]["links"]}
            links.discard(None)
            if len(links) != 1:
                raise ValueError("官方清冊明細連結與單號不一致；已停止。")
            self.rows.append(AppropriationRow(number, texts[1], texts[2],
                                              texts[3], texts[4], links.pop()))


def parse_list(body: bytes, content_type: str, final_url: str) -> tuple[list[AppropriationRow], set[int]]:
    url = urllib.parse.urlsplit(final_url)
    if ((url.scheme, url.hostname, url.path) != ("https", "cis.ncu.edu.tw", LIST_PATH)
            or "html" not in content_type.lower() or len(body) > MAX_BODY):
        raise ValueError("未取得官方主持人費清單；沒有列印。")
    parser = _ListParser()
    parser.feed(body.decode("utf-8", "replace"))
    if not parser.headings_found:
        raise ValueError("官方主持人費清單欄位未辨識；沒有列印。")
    numbers = [row.number for row in parser.rows]
    if len(numbers) != len(set(numbers)):
        raise ValueError("官方清單有重複單號；沒有列印。")
    return parser.rows, parser.pages


def fetch_list(opener, page: int = 1) -> tuple[list[AppropriationRow], set[int]]:
    if not 1 <= page <= 1000:
        raise ValueError("清單頁碼不正確；沒有列印。")
    url = LIST_URL if page == 1 else LIST_URL + f"&page={page}"
    response = open_page(opener, url, DEFAULT_USER_AGENT)
    body = response.read(MAX_BODY + 1)
    if response.getcode() != 200 or response.geturl() != url:
        raise ValueError("官方主持人費清單未正常載入；沒有列印。")
    return parse_list(body, response.headers.get("Content-Type", ""), response.geturl())


class _PrintLinkParser(HTMLParser):
    def __init__(self, detail_url: str, number: str) -> None:
        super().__init__(convert_charrefs=True)
        self.detail_url = detail_url
        self.number = number
        self.links: set[str] = set()
        self._anchor: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            self._anchor = {"attrs": dict(attrs), "text": []}

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._anchor is None:
            return
        anchor = self._anchor
        self._anchor = None
        if "列印" not in "".join(anchor["text"]):
            return
        onclick = anchor["attrs"].get("onclick") or ""
        match = re.fullmatch(r"\s*(?:javascript:)?window\.open\(\s*['\"]([^'\"]+)['\"]\s*\)\s*;?\s*",
                             onclick, re.I)
        if match:
            link = _official_url(match.group(1), self.detail_url, PRINT_PATH,
                                 self.number)
            if link:
                self.links.add(link)


def save_pdf(opener, row: AppropriationRow, folder: Path = OUTPUT_DIR) -> Path:
    if not NUMBER_RE.fullmatch(row.number) or _official_url(
            row.detail_url, LIST_URL, DETAIL_PATH, row.number) != row.detail_url:
        raise ValueError("清冊單號或明細網址不符；沒有列印。")
    response = open_page(opener, row.detail_url, DEFAULT_USER_AGENT, referer=LIST_URL)
    body = response.read(MAX_BODY + 1)
    if (response.getcode() != 200 or response.geturl() != row.detail_url
            or "html" not in response.headers.get("Content-Type", "").lower()
            or len(body) > MAX_BODY):
        raise ValueError("官方清冊明細未正常載入；沒有列印。")
    parser = _PrintLinkParser(row.detail_url, row.number)
    parser.feed(body.decode("utf-8", "replace"))
    if len(parser.links) != 1:
        raise ValueError("官方明細中找不到唯一的列印連結；沒有列印。")
    print_url = parser.links.pop()
    response = open_page(opener, print_url, DEFAULT_USER_AGENT, referer=row.detail_url)
    pdf = response.read(MAX_BODY + 1)
    if (response.getcode() != 200 or response.geturl() != print_url
            or "pdf" not in response.headers.get("Content-Type", "").lower()
            or len(pdf) > MAX_BODY or len(pdf) < 100
            or not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-1024:]):
        raise ValueError("官方列印結果不是完整 PDF；沒有保存檔案。")
    if folder.is_symlink():
        raise ValueError("PDF 資料夾不可為符號連結；沒有保存檔案。")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"印領清冊_{row.number}.pdf"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("同名 PDF 已存在；沒有覆蓋。") from exc
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(pdf)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
