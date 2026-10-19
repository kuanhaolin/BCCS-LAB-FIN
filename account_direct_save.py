"""Guarded, single-attempt direct save for the NCU petty-cash claim form.

Preparation reads the current legacy forms and performs only the official payee
lookup. It never calls the final save URL. A separate explicit action sends the
prepared Big5 form exactly once; uncertain outcomes must be checked manually.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import http.cookiejar
import json
import re
import ssl
import urllib.parse
import urllib.request

from account_session_probe import (
    ACCOUNT_FRAME_PATHS, HOST, MAX_BODY_BYTES, SameHostRedirect,
    appy_frame_url, cookie_jar,
)


BASE = "https://" + HOST
SAVE_PATH = "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp"
LOOKUP_PATH = "/APSWIS_Q/Dept_Add_Q/CHK_VEN_Q.asp"
CATEGORY_PATH = "/APSWIS_Q/Dept_Add_Q/STD_APPY_FRM_Q.asp"
TRAN_PATH = "/APSWIS_Q/Dept_Add_Q/TRAN_Q.asp"
USER_AGENT = "Mozilla/5.0"
SAVE_FIELDS = (
    "SAMPLENO", "APPU", "IsNew", "APPYYEAR", "APYNO", "APYADD", "ADATE",
    "APYDEP", "APYUSERNO", "ISAPPY", "ISVOUC", "ISTEMP", "ISCASH",
    "ISPURC", "APPYSET", "APPYBUY", "MOVETYPE", "SETNAME", "ABSTRACT",
    "D_STR", "A_STR", "P_STR", "ALL_DEPT", "NO_MATH", "VOUSERNO",
    "ISPERI", "ISESS", "Y_METHODS", "ACCORDING",
)
MAIN_COPY_FIELDS = (
    "SAMPLENO", "APPU", "IsNew", "APPYYEAR", "APYNO", "APYADD", "ADATE",
    "APYDEP", "APYUSERNO", "ISAPPY", "ISVOUC", "ISTEMP", "ISCASH",
    "ISPURC", "APPYSET", "MOVETYPE", "SETNAME", "NO_MATH", "ISPERI",
    "ISESS", "Y_METHODS", "ACCORDING",
)


@dataclass
class LegacyForm:
    name: str
    action: str
    method: str
    fields: dict[str, list[str]]
    options: dict[str, set[str]]

    def one(self, name: str) -> str:
        values = self.fields.get(name, [])
        if len(values) != 1:
            raise ValueError(f"官方頁面缺少或重複欄位 {name}；沒有存入。")
        return values[0]


class LegacyForms(HTMLParser):
    """Parse form controls without executing any script or loading resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[LegacyForm] = []
        self.current: LegacyForm | None = None
        self.select: str | None = None
        self.select_first: str | None = None
        self.select_selected: str | None = None
        self.textarea: str | None = None
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "form":
            self.current = LegacyForm(data.get("name") or data.get("id") or "",
                                      data.get("action") or "", data.get("method") or "get",
                                      {}, {})
            self.forms.append(self.current)
        elif self.current is not None and tag == "input":
            name = data.get("name")
            kind = (data.get("type") or "text").lower()
            if name and kind not in {"button", "submit", "reset", "image", "file"}:
                if kind not in {"checkbox", "radio"} or "checked" in data:
                    self.current.fields.setdefault(name, []).append(data.get("value") or "")
        elif self.current is not None and tag == "select":
            self.select = data.get("name")
            self.select_first = None
            self.select_selected = None
            if self.select:
                self.current.options[self.select] = set()
        elif self.current is not None and tag == "option" and self.select:
            value = data.get("value") or ""
            self.current.options[self.select].add(value)
            if self.select_first is None:
                self.select_first = value
            if "selected" in data:
                self.select_selected = value
        elif self.current is not None and tag == "textarea":
            self.textarea = data.get("name")
            self.text_parts = []

    def handle_data(self, data: str) -> None:
        if self.textarea is not None:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self.current is not None and self.select:
            self.current.fields.setdefault(self.select, []).append(
                self.select_selected if self.select_selected is not None else self.select_first or "")
            self.select = None
        elif tag == "textarea" and self.current is not None and self.textarea:
            self.current.fields.setdefault(self.textarea, []).append("".join(self.text_parts))
            self.textarea = None
            self.text_parts = []
        elif tag == "form":
            self.current = None


def parse_form(content: str, target_url: str, required: set[str], action_path: str) -> LegacyForm:
    parser = LegacyForms()
    parser.feed(content)
    matches = [form for form in parser.forms if required.issubset(form.fields)]
    if len(matches) != 1:
        raise ValueError("官方頁面找不到唯一的預期表單；沒有存入。")
    form = matches[0]
    action = urllib.parse.urlsplit(urllib.parse.urljoin(target_url, form.action))
    if (form.method.lower() != "post" or action.scheme != "https" or
            action.hostname != HOST or action.path != action_path):
        raise ValueError("官方表單的提交位置或方法已變更；沒有存入。")
    return form


def _read_expected(opener: urllib.request.OpenerDirector, url: str,
                   method: str = "GET", body: bytes | None = None,
                   referer: str = "") -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = BASE
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with opener.open(request, timeout=25) as response:
        if response.status != 200 or urllib.parse.urlsplit(response.geturl()).path != urllib.parse.urlsplit(url).path:
            raise ValueError("官方頁面未回到預期位置；沒有存入。")
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("官方頁面內容過大；沒有存入。")
    return raw.decode("cp950", errors="replace")


def _lookup_value(content: str, field: str) -> str:
    pattern = (r'parent\.MAIN\.APPA\.FORM1\.' + field +
               r'\.value\s*=\s*("(?:\\.|[^"\\])*")')
    matches = re.findall(pattern, content)
    if len(matches) != 1:
        raise ValueError("官方受款人回填格式已變更；沒有存入。")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise ValueError("官方受款人回填資料無法解析；沒有存入。") from exc
    if not isinstance(value, str):
        raise ValueError("官方受款人回填資料格式不正確；沒有存入。")
    return value


def _no_delimiter(value: str, label: str) -> str:
    if any(char in value for char in "#@◎\r\n"):
        raise ValueError(f"{label} 含有官方明細分隔字元；沒有存入。")
    return value


def _preloaded_budget_codes(content: str, budget_no: str) -> set[str]:
    """Read the values used by the official BN_1() client-side dropdown.

    BN_1() splits each T_A_BS entry as budget#code#description. We only
    inspect quoted entries in a script containing T_A_BS; no script is run.
    """
    codes: set[str] = set()
    prefix = re.compile(r'["\']' + re.escape(budget_no) + r'#([0-9]{3})#')
    for script in re.findall(r'<script\b[^>]*>(.*?)</script\s*>', content,
                             flags=re.IGNORECASE | re.DOTALL):
        if re.search(r'\bT_A_BS\b', script):
            codes.update(prefix.findall(script))
    return codes


def _trans_budget_codes(content: str, budget_no: str, appy_content: str) -> set[str]:
    """Read the exact source of APPY's T_A_BS: TRAN.FORM2.BUGETS.

    Derive the entry separator from the current APPY script, then compare each
    entry's plan and purpose fields without executing any page scripts.
    """
    separators = set(re.findall(
        r'\bT_A_BS\s*=\s*BUGETS_STR\.split\(\s*["\']([^"\'\\]{1,4})["\']\s*\)',
        appy_content,
    ))
    if len(separators) != 1 or next(iter(separators)) == "#":
        raise ValueError("官方 APPY 頁無法確認 BUGETS 分隔符號；沒有存入。")
    separator = next(iter(separators))
    parser = LegacyForms()
    parser.feed(content)
    matches = [form for form in parser.forms
               if form.name == "FORM2" and "BUGETS" in form.fields]
    if len(matches) != 1 or len(matches[0].fields["BUGETS"]) != 1:
        raise ValueError("官方 TRAN 頁缺少唯一的 BUGETS 欄位；沒有存入。")
    codes: set[str] = set()
    for entry in matches[0].fields["BUGETS"][0].split(separator):
        parts = entry.split("#", 2)
        if len(parts) == 3 and parts[0] == budget_no and re.fullmatch(r"[0-9]{3}", parts[1]):
            codes.add(parts[1])
    return codes


def _raw_page_signals(content: str, budget_no: str) -> str:
    """Only structural counts/booleans, never HTML or personal field values."""
    options = len(re.findall(r'<option\b', content, flags=re.IGNORECASE))
    raw_plan = bool(re.search(
        r'<option\b[^>]*\bvalue\s*=\s*["\']?' + re.escape(budget_no) + r'(?=["\'\s>])',
        content, flags=re.IGNORECASE,
    ))
    preload = bool(re.search(r'\bT_A_BS\b', content))
    pair = budget_no + "#300#" in content
    password = bool(re.search(r'<input\b[^>]*\btype\s*=\s*["\']?password\b',
                              content, flags=re.IGNORECASE))
    return (f"原始 option 標籤 {options}、原始計畫選項 {'有' if raw_plan else '無'}、"
            f"T_A_BS 標記 {'有' if preload else '無'}、"
            f"計畫與業務費資料片段 {'有' if pair else '無'}、"
            f"密碼欄位 {'有' if password else '無'}")


def _initialize_category(opener: urllib.request.OpenerDirector) -> None:
    """Replay only the observed CHK4/next-step POST, never the save POST."""
    year = date.today().year - 1911
    values = [
        ("APPYSET", "14"), ("MOVETYPE", "6"),
        ("SETNAME", "零用金先墊付報支（含國內旅費）"),
        ("ISAPPY", "True"), ("ISVOUC", "True"),
        ("ISTEMP", "False"), ("ISCASH", "True"),
        ("ISPURC", "False"), ("SHOWVEN", "True"),
        ("SHOWPRO", "True"), ("ISPERIOD", "False"),
        ("APPYYEAR", str(year)), ("ISNEW", "Y"),
        ("APYNO", ""), ("APYADD", ""), ("APPYBUY", ""),
        ("CHK4", "on"), ("SSS", "下一步"),
    ]
    body = urllib.parse.urlencode(values, encoding="cp950").encode("ascii")
    _read_expected(opener, BASE + CATEGORY_PATH, "POST", body,
                   BASE + "/APSWIS_Q/Dept_Add_Q/DA_SerBug_Menu_Q.asp?Firstload=Y")


@dataclass
class PreparedSave:
    jar: http.cookiejar.CookieJar
    context: ssl.SSLContext
    body: bytes
    budget_no: str
    amount: str
    payee_verified: bool


@dataclass(frozen=True)
class SaveAttemptResult:
    status: int
    path: str
    success_text_found: bool


def prepare_save(record: dict[str, str], cookies: dict[str, str],
                 context: ssl.SSLContext) -> PreparedSave:
    """Read current forms and perform one payee lookup; never call SAVE_PATH."""
    if record.get("advancePaid") != "yes" or record.get("budgetCode") != "300":
        raise ValueError("目前只支援業務費、代墊的零用金報支；沒有存入。")
    if record.get("budgetNo") != "11516004":
        raise ValueError("本機直接存入目前只支援已核對的計畫代碼；沒有存入。")
    amounts = [record.get(key, "") for key in ("budgetAmount", "productAmount", "payeeAmount")]
    if len(set(amounts)) != 1 or not re.fullmatch(r"[0-9]{1,10}", amounts[0]) or int(amounts[0]) <= 0:
        raise ValueError("三處金額必須相同且為正整數；沒有存入。")
    for key in ("abstract", "product", "invoiceNo"):
        _no_delimiter(record.get(key, ""), key)

    jar = cookie_jar(cookies)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context), SameHostRedirect(),
    )
    urls = {frame: (appy_frame_url() if frame == "APPY" else
                    BASE + path) for frame, path in ACCOUNT_FRAME_PATHS.items()}
    menu_url = BASE + "/APSWIS_Q/Dept_Add_Q/DA_SerBug_Menu_Q.asp?Firstload=Y"
    appy = _read_expected(opener, urls["APPY"], referer=menu_url)
    main_required = {"APYUSERNO", "BUGETNO_1", "BUGCODE_1", "D_AMOUNT_1", "APPYSET"}
    main = parse_form(appy, urls["APPY"], main_required, ACCOUNT_FRAME_PATHS["APPY"])
    preloaded_codes = _preloaded_budget_codes(appy, record["budgetNo"])
    trans_codes: set[str] = set()
    if (record["budgetNo"] not in main.options.get("BUGETNO_1", set()) and
            not preloaded_codes):
        # APPY's browser script obtains its budget/code pairs from this hidden
        # frame. A raw APPY GET need not contain the rendered <option> tags.
        trans_url = BASE + TRAN_PATH + "?COM=NT"
        trans = _read_expected(opener, trans_url, referer=menu_url)
        trans_codes = _trans_budget_codes(trans, record["budgetNo"], appy)
        # The observed CHK4/next-step POST initializes the category; it is not
        # the final reimbursement save request.
        _initialize_category(opener)
        appy = _read_expected(opener, urls["APPY"], referer=BASE + CATEGORY_PATH)
        main = parse_form(appy, urls["APPY"], main_required, ACCOUNT_FRAME_PATHS["APPY"])
        preloaded_codes = _preloaded_budget_codes(appy, record["budgetNo"])
    pages = {"APPY": appy}
    pages.update({frame: _read_expected(opener, url, referer=urls["APPY"])
                  for frame, url in urls.items() if frame != "APPY"})
    item = parse_form(pages["APPP"], urls["APPP"],
                      {"PRODUCT_1", "AMOUNT_1"}, ACCOUNT_FRAME_PATHS["APPP"])
    payee = parse_form(pages["APPA"], urls["APPA"],
                       {"VENNAME_1", "VENDORID_1", "AMOUNT_1", "PAYKIND_1"},
                       ACCOUNT_FRAME_PATHS["APPA"])
    save = parse_form(pages["PS"], urls["PS"], set(SAVE_FIELDS), SAVE_PATH)
    if (main.one("APPYSET") != "14" or main.one("MOVETYPE") != "6" or
            main.one("MOVETYPE_1") != "6" or main.one("NO_MATH") != "N" or
            main.one("APPYBUY") != "" or main.one("IsNew") != "Y" or
            main.one("ALL_DEPT") != "" or save.one("APPYBUY") != "" or
            save.one("ALL_DEPT") != ""):
        raise ValueError("官方頁面不是已核對的新增零用金代墊表單；沒有存入。")
    if main.one("D_APYKIND_1") or main.one("SUBJECTNO_1"):
        raise ValueError("官方經費明細含尚未核對的類別或科目；沒有存入。")
    budget_options = main.options.get("BUGETNO_1", set())
    code_options = main.options.get("BUGCODE_1", set())
    budget_found = record["budgetNo"] in budget_options or bool(preloaded_codes or trans_codes)
    code_found = ("300" in preloaded_codes or "300" in trans_codes or
                  record["budgetNo"] in budget_options and "300" in code_options)
    if not budget_found or not code_found:
        budget_state = "有" if budget_found else "沒有"
        code_state = "有" if code_found else "沒有"
        raise ValueError(
            f"官方經費頁目前{budget_state}所選計畫選項、{code_state}業務費 300 選項"
            f"（選項數：計畫 {len(budget_options)}、經費用途 {len(code_options)}；"
            f"APPY 預載代碼數 {len(preloaded_codes)}、TRAN 配對代碼數 {len(trans_codes)}）。"
            f"頁面結構：{_raw_page_signals(appy, record['budgetNo'])}。沒有存入。"
        )
    applicant_id = main.one("APYUSERNO")
    if not re.fullmatch(r"[A-Za-z0-9]{5,20}", applicant_id):
        raise ValueError("官方申請人識別欄位格式不正確；沒有存入。")
    if not main.one("APYDEP") or not main.one("ADATE"):
        raise ValueError("官方申請日期或單位欄位缺失；沒有存入。")

    lookup_url = BASE + LOOKUP_PATH
    lookup_body = urllib.parse.urlencode({
        "R_Sign": "Y", "VENDORID": applicant_id,
        "RE_HAND": "parent.MAIN.APPA.FORM1", "I_J": "_1", "PAYKIND": "2",
    }).encode("ascii")
    lookup = _read_expected(opener, lookup_url, "POST", lookup_body, lookup_url)
    id_match = re.search(
        r'parent\.MAIN\.APPA\.FORM1\.VENDORID_1\.value\s*=\s*CharToAsc\("([A-Za-z0-9]{5,20})"\)',
        lookup,
    )
    if not id_match or id_match.group(1) != applicant_id:
        raise ValueError("官方受款人身分與申請人不一致；沒有存入。")
    payee_name = _no_delimiter(_lookup_value(lookup, "VENNAME_1"), "受款人姓名")
    if not payee_name.strip():
        raise ValueError("官方未帶入受款人姓名；沒有存入。")
    bank = [_no_delimiter(_lookup_value(lookup, name), name)
            for name in ("BANKNO_1", "ACCOUNT_1", "ACCOUNTNAM_1")]

    main_fields = {name: main.one(name) for name in MAIN_COPY_FIELDS}
    main_fields["ABSTRACT"] = record["abstract"]
    main_fields["VOUSERNO"] = main.one("VOU_SERNO")
    main_fields["D_STR"] = "#".join((
        "1", record["budgetNo"], "300", record["budgetAmount"],
        _no_delimiter(main.one("D_APYKIND_1"), "經費類別"),
        _no_delimiter(main.one("SUBJECTNO_1"), "科目"),
        main.one("MOVETYPE_1"),
    )) + "@"
    main_fields["A_STR"] = "#".join((
        "1", applicant_id, payee_name, record["invoiceDate"], record["invoiceNo"],
        record["payeeAmount"], _no_delimiter(payee.one("TAX_1"), "稅額"), "2",
        *bank, _no_delimiter(payee.one("I_VENDORID_1"), "開立人識別"), "",
    )) + "@"
    main_fields["P_STR"] = "◎".join((
        "1", record["product"],
        *(_no_delimiter(item.one(name), name) for name in (
            "QUANTITY_1", "SERUNIT_1")),
        record["productAmount"],
        *(_no_delimiter(item.one(name), name) for name in (
            "PLACE_1", "PTYPE_1", "KEEPER_1", "KEEPERID_1", "KEEPERDNO_1")),
    )) + "@"
    payload = {name: save.one(name) for name in SAVE_FIELDS}
    payload.update(main_fields)
    try:
        body = urllib.parse.urlencode(
            [(name, payload[name]) for name in SAVE_FIELDS], encoding="cp950",
            errors="strict", quote_via=urllib.parse.quote_plus,
        ).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("摘要或品名含報帳系統不支援字元；沒有存入。") from exc
    return PreparedSave(jar, context, body, record["budgetNo"],
                        record["budgetAmount"], True)


def send_save_once(prepared: PreparedSave) -> SaveAttemptResult:
    """Send one final POST. The caller must guard against retries first."""
    class StopRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(prepared.jar),
        urllib.request.HTTPSHandler(context=prepared.context), StopRedirect(),
    )
    target = BASE + SAVE_PATH
    request = urllib.request.Request(
        target, data=prepared.body,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": BASE, "Referer": BASE + "/APSWIS_Q/Dept_Add_Q/PR_SAVE_Q.asp"},
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        raw = response.read(MAX_BODY_BYTES + 1)
        status = response.status
        path = urllib.parse.urlsplit(response.geturl()).path
    if len(raw) > MAX_BODY_BYTES:
        return SaveAttemptResult(status, path, False)
    content = raw.decode("cp950", errors="replace")
    return SaveAttemptResult(status, path,
                             status == 200 and path == SAVE_PATH and "資料已存入" in content)
