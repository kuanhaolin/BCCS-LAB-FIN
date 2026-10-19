"""Guarded ResearchSys 甲式 roster printing."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import urllib.parse

from research_budget import MAX_BODY, ORIGIN, OUTPUT_DIR, _get


SUMMARY_PATH = "/ResearchSys/budget/budgetHumanSummary"
HUMAN_PATH = "/ResearchSys/budget/budgetHuman"
SUMMARY_URL = ORIGIN + SUMMARY_PATH
HUMAN_URL = ORIGIN + HUMAN_PATH
ID_RE = re.compile(r"[0-9]{1,20}\Z")
KEY_RE = re.compile(r"BH_[0-9]{1,20}\Z")
MONTH_RE = re.compile(r"[0-9]{5}\Z")


@dataclass(frozen=True)
class HumanPerson:
    key: str
    label: str


class _SummaryLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.budget_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        raw = dict(attrs).get("href") or ""
        url = urllib.parse.urlsplit(urllib.parse.urljoin(SUMMARY_URL, raw))
        query = urllib.parse.parse_qs(url.query)
        if (url.scheme == "https" and url.netloc == "cis.ncu.edu.tw"
                and url.path == HUMAN_PATH and not url.fragment
                and set(query) == {"budgetId"} and len(query["budgetId"]) == 1
                and ID_RE.fullmatch(query["budgetId"][0])):
            self.budget_ids.add(query["budgetId"][0])


def summary_budget_ids(opener) -> set[str]:
    body, content_type = _get(opener, SUMMARY_URL)
    if "html" not in content_type.lower():
        raise ValueError("官方人員名冊清單不是 HTML；沒有列印。")
    parser = _SummaryLinks()
    parser.feed(body.decode("utf-8", "replace"))
    if not parser.budget_ids:
        raise ValueError("官方人員名冊清單沒有可辨識的計畫；沒有列印。")
    return parser.budget_ids


class _HumanPrintForm(HTMLParser):
    def __init__(self, budget_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.budget_id = budget_id
        self.forms: list[list[HumanPerson]] = []
        self.in_form = False
        self.fields: dict[str, str] = {}
        self.has_start = self.has_end = self.has_submit = self.has_a_type = False
        self.div_depth = 0
        self.other_depth = 0
        self.row_key = ""
        self.row_cells: list[str] = []
        self.cell_parts: list[str] | None = None
        self.people: list[HumanPerson] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            url = urllib.parse.urlsplit(urllib.parse.urljoin(HUMAN_URL, a.get("action") or ""))
            self.in_form = (a.get("method", "get").lower() == "get"
                            and url.scheme == "https" and url.netloc == "cis.ncu.edu.tw"
                            and url.path == HUMAN_PATH and not url.query and not url.fragment)
            self.fields = {}
            self.has_start = self.has_end = self.has_submit = self.has_a_type = False
            self.div_depth = self.other_depth = 0
            self.people = []
            return
        if not self.in_form:
            return
        if tag == "div":
            self.div_depth += 1
            if a.get("id") == "other":
                self.other_depth = self.div_depth
        elif tag == "tr" and self.other_depth:
            self.row_key, self.row_cells = "", []
        elif tag == "td" and self.other_depth:
            self.cell_parts = []
        elif tag == "input":
            name = a.get("name") or ""
            if a.get("type", "").lower() == "hidden" and name in ("action", "budgetId"):
                self.fields[name] = a.get("value") or ""
            elif name == "salaryStartDate" and a.get("id") == name:
                self.has_start = True
            elif name == "salaryEndDate" and a.get("id") == name:
                self.has_end = True
            elif (name == "Submit" and a.get("id") == "printList"
                  and a.get("type", "").lower() == "submit" and a.get("value") == "列印"):
                self.has_submit = True
            elif (self.other_depth and name == "keyList[]"
                  and a.get("type", "").lower() == "checkbox"
                  and "keyList" in (a.get("class") or "").split()):
                self.row_key = a.get("value") or ""
        elif tag == "option" and a.get("value") == "a_type":
            self.has_a_type = True

    def handle_data(self, data):
        if self.cell_parts is not None:
            self.cell_parts.append(data)

    def handle_endtag(self, tag):
        if not self.in_form:
            return
        if tag == "td" and self.cell_parts is not None:
            self.row_cells.append(" ".join("".join(self.cell_parts).split()))
            self.cell_parts = None
        elif tag == "tr" and self.other_depth and self.row_key:
            if KEY_RE.fullmatch(self.row_key) and len(self.row_cells) >= 5:
                label = " · ".join(value for value in
                                   (self.row_cells[1], self.row_cells[4]) if value)
                if label:
                    self.people.append(HumanPerson(self.row_key, label))
            self.row_key = ""
        elif tag == "div" and self.div_depth:
            if self.other_depth == self.div_depth:
                self.other_depth = 0
            self.div_depth -= 1
        elif tag == "form":
            if (self.fields == {"action": "print", "budgetId": self.budget_id}
                    and self.has_start and self.has_end and self.has_submit
                    and self.has_a_type and self.people):
                self.forms.append(self.people)
            self.in_form = False


def fetch_human_people(opener, budget_id: str) -> list[HumanPerson]:
    if not ID_RE.fullmatch(budget_id):
        raise ValueError("計畫識別碼格式不正確；沒有列印。")
    if budget_id not in summary_budget_ids(opener):
        raise ValueError("官方人員名冊清單沒有此計畫；沒有列印。")
    body, content_type = _get(opener, HUMAN_URL + "?budgetId=" + budget_id)
    if "html" not in content_type.lower():
        raise ValueError("官方人員名冊不是 HTML；沒有列印。")
    parser = _HumanPrintForm(budget_id)
    parser.feed(body.decode("utf-8", "replace"))
    if len(parser.forms) != 1:
        raise ValueError("找不到唯一的官方甲式列印表單；沒有列印。")
    people = parser.forms[0]
    if len({person.key for person in people}) != len(people):
        raise ValueError("官方甲式人員代碼重複；沒有列印。")
    return people


def _month(value: str) -> bool:
    return bool(MONTH_RE.fullmatch(value) and 1 <= int(value[-2:]) <= 12)


def save_human_a_pdf(opener, budget_id: str, person_key: str,
                     start: str, end: str, folder: Path = OUTPUT_DIR) -> Path:
    if not _month(start) or not _month(end) or start > end:
        raise ValueError("甲式起迄年月須為民國五碼且起月不晚於迄月；沒有列印。")
    if not KEY_RE.fullmatch(person_key):
        raise ValueError("甲式人員代碼格式不正確；沒有列印。")
    if folder.is_symlink():
        raise ValueError("PDF 資料夾不可為符號連結；沒有列印。")
    path = folder / "甲式.pdf"
    if path.exists():
        raise ValueError("甲式.pdf 已存在；沒有覆蓋，請先檢查資料夾。")
    people = fetch_human_people(opener, budget_id)
    if person_key not in {person.key for person in people}:
        raise ValueError("官方甲式表單沒有選取的人員；沒有列印。")
    query = urllib.parse.urlencode((
        ("action", "print"), ("budgetId", budget_id), ("printType", "a_type"),
        ("salaryStartDate", start), ("salaryEndDate", end),
        ("Submit", "列印"), ("keyList[]", person_key),
    ))
    pdf, content_type = _get(opener, HUMAN_URL + "?" + query)
    if ("application/pdf" not in content_type.lower()
            or not (100 < len(pdf) <= MAX_BODY)
            or not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-1024:]):
        raise ValueError("官方甲式列印結果不是完整 PDF；沒有保存檔案。")
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("甲式.pdf 已存在；沒有覆蓋，請先檢查資料夾。") from exc
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(pdf)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
