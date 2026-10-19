"""Read-only purchase-management listing for supported reimbursements."""

from __future__ import annotations

from dataclasses import dataclass
import html
from html.parser import HTMLParser
import re
import ssl
import urllib.parse
import urllib.request

from account_direct_save import BASE, LegacyForms, USER_AGENT
from account_session_probe import HOST, MAX_BODY_BYTES, SameHostRedirect, cookie_jar


LIST_PATH = "/APSWIS_Q/Dept_Add_Q/DA_SerSTART_Q.asp"
LIST_URL = BASE + LIST_PATH
TITLE_PATH = "/APSWIS_Q/Dept_Add_Q/DA_SerFun_Q.asp"
TITLE_URL = BASE + TITLE_PATH + "?COM=NT"
PRINT_PATH = "/APSWIS_Q/TMP_PAG_Q/NCU33.asp"


@dataclass(frozen=True)
class PurchaseRow:
    number: str
    date: str
    summary: str
    amount: str
    status: str
    addition: str = ""
    category: str = "14"
    print_href: str = "NCU33.asp"


def purchase_print_url(row: PurchaseRow, roc_year: int) -> str:
    """Construct only the print URL observed in the official PRT button."""
    if (not 1 <= roc_year <= 999 or not re.fullmatch(r"\d{1,20}", row.number)
            or not re.fullmatch(r"[A-Za-z0-9]{0,20}", row.addition)
            or row.category not in ("14", "15")
            or not re.fullmatch(r"NCU\d{1,3}\.asp", row.print_href)):
        raise ValueError("案件列印參數無法核對。")
    query = urllib.parse.urlencode({
        "APPYYEAR": str(roc_year), "APYNO": row.number,
        "APYADD": row.addition, "APPYSET": row.category,
    })
    return BASE + "/APSWIS_Q/TMP_PAG_Q/" + row.print_href + "?" + query


class CellText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self.in_cell = False
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "td":
            self.cells.append("")
            self.in_cell = True
        elif tag == "script":
            self.in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self.in_cell = False
        elif tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_cell and not self.in_script:
            self.cells[-1] += data


def _clean(value: str) -> str:
    return " ".join(value.split())


def parse_purchase_rows(content: str, category: str = "14") -> list[PurchaseRow]:
    if category not in ("14", "15"):
        raise ValueError("不支援的報支類別；未列出案件。")
    rows: list[PurchaseRow] = []
    seen: set[tuple[str, str]] = set()
    scripts = list(re.finditer(r"function\s+MR_\d+\s*\(\)\s*\{", content, re.I))
    for script in scripts:
        start = content.rfind("<tr", 0, script.start())
        end = content.find("</tr>", script.end())
        if start < 0 or end < 0:
            raise ValueError("官方案件列的結構無法辨識；未列出案件。")
        row_html = content[start:end + len("</tr>")]
        number_match = re.search(r'FORM1\.APYNO\.value\s*=\s*"(\d{1,20})"', row_html)
        addition_match = re.search(r'FORM1\.APYADD\.value\s*=\s*"([A-Za-z0-9]{0,20})"', row_html)
        category_match = re.search(r'FORM1\.APPYSET\.value\s*=\s*"(\d+)"', row_html)
        print_match = re.search(r'FORM1\.PR_Href\.value\s*=\s*"([A-Za-z0-9_.]+)"', row_html)
        if (not number_match or not addition_match or not category_match
                or category_match.group(1) != category):
            raise ValueError("官方案件單號或類別無法核對；未列出案件。")
        if not print_match or not re.fullmatch(r"NCU\d{1,3}\.asp", print_match.group(1)):
            raise ValueError("官方案件列印格式已變更；未列出案件。")
        number = number_match.group(1)
        addition = addition_match.group(1)
        parser = CellText()
        parser.feed(row_html)
        cells = [_clean(cell) for cell in parser.cells]
        if (len(cells) < 7 or number not in cells[0]
                or not re.fullmatch(r"\d{6,7}", cells[2])
                or not re.fullmatch(r"[\d,]+", cells[4])):
            raise ValueError("官方案件欄位已變更；未列出案件。")
        if (number, addition) in seen:
            raise ValueError("官方清單有重複單號；未列出案件。")
        seen.add((number, addition))
        rows.append(PurchaseRow(number, cells[2], cells[3], cells[4], cells[6],
                                addition, category, print_match.group(1)))
    return rows


def _form(content: str, source_url: str):
    parser = LegacyForms()
    parser.feed(content)
    matches = [form for form in parser.forms
               if {"APPYYEAR", "APPYSET", "CHKVAL", "CHKVAL_S"}.issubset(form.fields)]
    if len(matches) != 1:
        raise ValueError("官方購案管理表單無法辨識；未列出案件。")
    form = matches[0]
    action = urllib.parse.urlsplit(urllib.parse.urljoin(source_url, form.action))
    if (form.method.lower() != "post" or action.scheme != "https"
            or action.hostname != HOST or action.path.lower() != LIST_PATH.lower()):
        raise ValueError("官方購案管理表單目標已變更；未列出案件。")
    return form, urllib.parse.urlunsplit(action)


def _read(opener, url: str, body: bytes | None = None) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if body is not None:
        headers.update({"Content-Type": "application/x-www-form-urlencoded",
                        "Origin": BASE, "Referer": LIST_URL})
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
    with opener.open(request, timeout=25) as response:
        if (response.status != 200 or
                urllib.parse.urlsplit(response.geturl()).path.lower() != LIST_PATH.lower()):
            raise ValueError("官方購案管理頁未正常載入；未列出案件。")
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("官方購案管理頁過大；未列出案件。")
    return raw.decode("cp950", errors="replace")


def _official_listing_entry(opener, roc_year: int) -> str:
    """Follow the purchase-management link generated by the official title frame.

    Opening DA_SerSTART_Q.asp without its per-session RNK can return an empty
    list even for an authenticated user with purchases.
    """
    request = urllib.request.Request(
        TITLE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    with opener.open(request, timeout=25) as response:
        if (response.status != 200 or
                urllib.parse.urlsplit(response.geturl()).path.lower() != TITLE_PATH.lower()):
            raise ValueError("官方購案管理選單未正常載入；未列出案件。")
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("官方購案管理選單過大；未列出案件。")
    content = html.unescape(raw.decode("cp950", errors="replace"))
    entries: set[str] = set()
    for match in re.finditer(r"DA_SerSTART_Q\.asp\?[^\s\x27\x22<>)]*", content, re.I):
        url = urllib.parse.urljoin(TITLE_URL, match.group())
        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != "https" or parsed.hostname != HOST or
                parsed.path.lower() != LIST_PATH.lower() or parsed.fragment or
                set(params) != {"RNK", "APPYYEAR"} or
                len(params["RNK"]) != 1 or len(params["APPYYEAR"]) != 1 or
                not re.fullmatch(r"[A-Za-z0-9]{1,40}", params["RNK"][0]) or
                params["APPYYEAR"][0] != str(roc_year)):
            continue
        entries.add(url)
    if len(entries) != 1:
        raise ValueError("官方購案管理連結無法唯一核對；未列出案件。")
    return entries.pop()


def _submit_filter(opener, content: str, source_url: str,
                   overrides: dict[str, str]) -> str:
    form, action = _form(content, source_url)
    for name in overrides:
        if len(form.fields.get(name, [])) != 1:
            raise ValueError("官方購案管理篩選欄位已變更；未列出案件。")
    pairs = [(name, overrides.get(name, value))
             for name, values in form.fields.items() for value in values]
    body = urllib.parse.urlencode(pairs, encoding="cp950", errors="strict").encode("ascii")
    return _read(opener, action, body)


def fetch_purchase_list(cookies: dict[str, str], context: ssl.SSLContext,
                        roc_year: int, category: str = "14") -> list[PurchaseRow]:
    if not 1 <= roc_year <= 999:
        raise ValueError("民國年份不正確；未查詢案件。")
    if category not in ("14", "15"):
        raise ValueError("不支援的報支類別；未查詢案件。")
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(cookies)),
        urllib.request.HTTPSHandler(context=context), SameHostRedirect(),
    )
    source_url = _official_listing_entry(opener, roc_year)
    content = _read(opener, source_url)
    form, _ = _form(content, source_url)
    if form.one("APPYSET") != category or form.one("APPYYEAR") != str(roc_year):
        content = _submit_filter(opener, content, source_url, {
            "APPYYEAR": str(roc_year), "APPYSET": category, "APYNO": "", "APYADD": "",
        })
        source_url = LIST_URL
        form, _ = _form(content, source_url)
    if form.one("CHKVAL_S") != "4" or form.one("CHKVAL") != "4":
        content = _submit_filter(opener, content, source_url,
                                 {"CHKVAL_S": "4", "CHKVAL": "4"})
        form, _ = _form(content, LIST_URL)
    if (form.one("APPYSET") != category or form.one("CHKVAL_S") != "4"
            or form.one("APPYYEAR") != str(roc_year)):
        raise ValueError("官方頁面沒有套用指定類別／未審核篩選；未列出案件。")
    return parse_purchase_rows(content, category)
