"""Parse the HumanSys business-application list without changing any records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import re
import urllib.parse


LIST_URL = "https://cis.ncu.edu.tw/HumanSys/personal/personalAttendApply/businessApply"
FIELDS = (
    "申請單號", "填單人", "申請人", "起日", "開始時間", "迄日", "結束時間",
    "本次申請時數", "給付方式", "表單狀態", "異動人",
)


@dataclass(frozen=True)
class BusinessRow:
    values: dict[str, str]
    submit_action: str | None
    submit_token: str | None

    @property
    def application_id(self) -> str:
        return self.values["申請單號"]


class _ListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.rows: list[BusinessRow] = []
        self._cells: dict[str, str] | None = None
        self._table_depth = 0
        self._headers: tuple[str, ...] | None = None
        self._row_cells: list[str] = []
        self._row_headings: list[str] = []
        self._cell_tag: str | None = None
        self._cell_name: str | None = None
        self._cell_text: list[str] = []
        self._form: dict[str, object] | None = None
        self._forms: list[dict[str, object]] = []
        self._button_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "table":
            if self._table_depth == 0:
                self._headers = None
            self._table_depth += 1
        elif tag == "tr":
            self._cells = {}
            self._row_cells = []
            self._row_headings = []
            self._forms = []
        elif tag in ("td", "th") and self._cells is not None:
            self._cell_tag = tag
            self._cell_name = attrs.get("data-th")
            self._cell_text = []
        elif tag == "form" and self._cells is not None:
            self._form = {
                "action": attrs.get("action", ""),
                "method": attrs.get("method", "get").lower(),
                "fields": [],
                "buttons": [],
            }
            self._forms.append(self._form)
        elif tag == "input" and self._form is not None:
            if attrs.get("name"):
                fields = self._form["fields"]
                assert isinstance(fields, list)
                fields.append((attrs["name"], attrs.get("value", "")))
        elif tag == "button" and self._form is not None:
            if attrs.get("type", "submit").lower() == "submit":
                self._button_text = []

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_text.append(data)
        if self._button_text is not None:
            self._button_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "button" and self._button_text is not None and self._form is not None:
            buttons = self._form["buttons"]
            assert isinstance(buttons, list)
            buttons.append(" ".join("".join(self._button_text).split()))
            self._button_text = None
        elif tag in ("td", "th") and self._cells is not None:
            value = " ".join("".join(self._cell_text).split())
            if self._cell_name:
                self._cells[self._cell_name] = value
            if tag == "td":
                self._row_cells.append(value)
            else:
                self._row_headings.append(value)
            self._cell_tag = None
            self._cell_name = None
            self._cell_text = []
        elif tag == "form":
            self._form = None
        elif tag == "tr" and self._cells is not None:
            if (len(self._row_headings) >= len(FIELDS)
                    and tuple(self._row_headings[:len(FIELDS)]) == FIELDS):
                self._headers = tuple(self._row_headings)
            values = dict(self._cells)
            if (not all(name in values for name in FIELDS)
                    and self._headers is not None
                    and len(self._row_cells) == len(self._headers)):
                values = dict(zip(self._headers, self._row_cells))
            if all(name in values for name in FIELDS):
                action, token = self._submit_form()
                self.rows.append(BusinessRow(values, action, token))
            self._cells = None
            self._forms = []
        elif tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            if self._table_depth == 0:
                self._headers = None

    def _submit_form(self) -> tuple[str | None, str | None]:
        matches = []
        for form in self._forms:
            buttons = form["buttons"]
            assert isinstance(buttons, list)
            if "送審" not in buttons or form["method"] != "post":
                continue
            action = urllib.parse.urljoin(self.base_url, str(form["action"]))
            fields = form["fields"]
            assert isinstance(fields, list)
            if len(fields) != 1 or fields[0][0] != "_token":
                continue
            matches.append((action, fields[0][1]))
        if len(matches) == 1:
            return matches[0]
        return None, None


def parse_list(body: bytes, content_type: str, final_url: str) -> list[BusinessRow]:
    parsed = urllib.parse.urlsplit(final_url)
    if (parsed.scheme, parsed.hostname, parsed.path) != (
        "https", "cis.ncu.edu.tw", "/HumanSys/personal/personalAttendApply/businessApply"
    ):
        raise ValueError("未到達 HumanSys 公差申請清單，已停止。")
    if "html" not in content_type.lower():
        raise ValueError("公差申請清單不是 HTML，已停止。")
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    parser = _ListParser(final_url)
    parser.feed(body.decode(charset, errors="replace"))
    return parser.rows


def validate_roc_date(value: str) -> str:
    if not re.fullmatch(r"\d{7}", value):
        raise ValueError("請使用七位民國日期，例如 1151027。")
    try:
        date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:7]))
    except ValueError as exc:
        raise ValueError("民國日期不存在。") from exc
    return value


def rows_for_start_date(rows: list[BusinessRow], roc_date: str) -> list[BusinessRow]:
    return [row for row in rows if row.values["起日"] == roc_date]


def list_html_diagnostics(body: bytes) -> dict[str, int | bool]:
    """Structural counts only; do not echo private row contents or tokens."""
    text = body.decode("utf-8", errors="replace")
    return {
        "html_tr_count": len(re.findall(r"<tr\b", text, re.I)),
        "html_data_th_count": len(re.findall(r"\bdata-th\s*=", text, re.I)),
        "html_application_number_cells": len(re.findall(
            r"\bdata-th\s*=\s*['\"]申請單號['\"]", text, re.I
        )),
        "mentions_ajax": bool(re.search(r"\bajax\b|\bdatatables?\b", text, re.I)),
    }


def selected_submit_form(row: BusinessRow, expected_date: str) -> tuple[str, bytes]:
    """Return one exact, currently-unsent record's POST target and body."""
    application_id = row.application_id
    if not re.fullmatch(r"\d+", application_id):
        raise ValueError("申請單號不是純數字，已停止。")
    if row.values["起日"] != expected_date:
        raise ValueError("起日與選擇條件不符，已停止。")
    if row.values["表單狀態"] != "尚未送出":
        raise ValueError("表單不是「尚未送出」，不能再次送審。")
    if not row.submit_action or not row.submit_token:
        raise ValueError("找不到此筆專屬的送審表單與 _token，已停止。")
    parsed = urllib.parse.urlsplit(row.submit_action)
    expected_path = (
        "/HumanSys/personal/personalAttendApply/"
        f"businessApply_dataToEform/{application_id}"
    )
    if (parsed.scheme, parsed.hostname, parsed.path, parsed.query, parsed.fragment) != (
        "https", "cis.ncu.edu.tw", expected_path, "", ""
    ):
        raise ValueError("送審網址與申請單號不一致，已停止。")
    return row.submit_action, urllib.parse.urlencode({"_token": row.submit_token}).encode("utf-8")
