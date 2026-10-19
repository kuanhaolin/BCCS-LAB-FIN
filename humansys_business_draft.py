"""Guarded HumanSys business-apply draft save, using an existing logged-in opener.

This implements only the narrow teacher / same-applicant / 08:00–12:00 case.
The website's actual save payload was unavailable, so a live save must be treated
as an experimental attempt and its result checked in the HumanSys record list.
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
import mimetypes
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request


CREATE_PATH = "/HumanSys/personal/personalAttendApply/businessApply/create"
SAVE_PATH = "/HumanSys/personal/personalAttendApply/businessApply/update"
LIST_PATH = "/HumanSys/personal/personalAttendApply/businessApply"
API_PREFIX = "https://cis.ncu.edu.tw/HumanSys/personal/attendApi/attendApi/"


class HiddenFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        attributes = dict(attrs)
        if tag.lower() == "form":
            self.current = {"action": attributes.get("action", ""), "hidden": {}}
            self.forms.append(self.current)
        elif tag.lower() == "input" and self.current is not None:
            if attributes.get("type", "text").lower() == "hidden" and attributes.get("name"):
                hidden = self.current["hidden"]
                assert isinstance(hidden, dict)
                hidden[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self.current = None


def _read_json(opener, path: str, user_agent: str) -> object:
    url = API_PREFIX + path
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Referer": "https://cis.ncu.edu.tw" + CREATE_PATH,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response = opener.open(request, timeout=30)
    final = urllib.parse.urlsplit(response.geturl())
    if final.hostname != "cis.ncu.edu.tw" or final.path != urllib.parse.urlsplit(url).path:
        raise ValueError("取得申請人資料時被導向其他頁面；未送出。")
    try:
        return json.loads(response.read(2_000_000))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("申請人 API 未回傳有效 JSON；未送出。") from exc


def _multipart(fields: dict[str, str], attachments: list[Path]) -> tuple[bytes, str]:
    boundary = "----HumanSysDraft" + secrets.token_hex(16)
    output = bytearray()
    for name, value in fields.items():
        output.extend(f"--{boundary}\r\n".encode("ascii"))
        output.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        output.extend(value.encode("utf-8"))
        output.extend(b"\r\n")
    for path in attachments:
        output.extend(f"--{boundary}\r\n".encode("ascii"))
        output.extend(
            f'Content-Disposition: form-data; name="ConfirmFileNfs[]"; filename="{path.name}"\r\n'
            .encode("utf-8")
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        output.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        output.extend(path.read_bytes())
        output.extend(b"\r\n")
    output.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(output), f"multipart/form-data; boundary={boundary}"


def inspect_applicant(opener, create_html: bytes, create_url: str,
                      user_agent: str) -> dict[str, object]:
    """Read-only comparison of identity fields; never return identifier values."""
    page = create_html.decode("utf-8", errors="replace")
    parser = HiddenFormParser()
    parser.feed(page)
    hidden = None
    for form in parser.forms:
        action = urllib.parse.urljoin(create_url, str(form["action"]))
        if urllib.parse.urlsplit(action).path == SAVE_PATH:
            hidden = form["hidden"]
            break
    if not isinstance(hidden, dict):
        raise ValueError("找不到公差申請表單。")
    info = _read_json(opener, "getApplyInfo", user_agent)
    if not isinstance(info, dict):
        raise ValueError("getApplyInfo 未回傳物件。")
    html_filer = str(hidden.get("ApplyPersonalEncode") or "")
    html_applicant = str(hidden.get("PersonalNoEncode") or "")
    api_filer = str(info.get("PersonalNoEncode") or "")
    normalize = lambda value: value.strip().lower()
    return {
        "html_filer_present": bool(html_filer),
        "html_applicant_present": bool(html_applicant),
        "api_filer_present": bool(api_filer),
        "html_filer_matches_api": bool(html_filer and api_filer and html_filer == api_filer),
        "html_filer_matches_api_normalized": bool(
            html_filer and api_filer and normalize(html_filer) == normalize(api_filer)
        ),
        "html_applicant_matches_api": bool(
            html_applicant and api_filer and normalize(html_applicant) == normalize(api_filer)
        ),
        "html_filer_matches_applicant": bool(
            html_filer and html_applicant and normalize(html_filer) == normalize(html_applicant)
        ),
        "parttime_agent": bool(info.get("ParttimeAgent")),
        "html_filer_length": len(html_filer),
        "api_filer_length": len(api_filer),
    }


def build_draft_request(opener, create_html: bytes, create_url: str,
                        prepared: dict[str, object], user_agent: str) -> urllib.request.Request:
    """Run all read-only checks and build, but do not send, the save request."""
    parsed_url = urllib.parse.urlsplit(create_url)
    if parsed_url.hostname != "cis.ncu.edu.tw" or parsed_url.path != CREATE_PATH:
        raise ValueError("目前不是公差申請新增頁；未送出。")
    if (prepared["start_date"] != prepared["end_date"]
            or prepared["start_time"] != "08:00"
            or prepared["end_time"] != "12:00"
            or prepared["elapsed_hours"] != 4
            or prepared["include_holiday"] != "N"):
        raise ValueError("目前儲存程式只支援同一天 08:00–12:00、假日選 N；未送出。")

    page = create_html.decode("utf-8", errors="replace")
    parser = HiddenFormParser()
    parser.feed(page)
    candidate = None
    for form in parser.forms:
        action = urllib.parse.urljoin(create_url, str(form["action"]))
        if urllib.parse.urlsplit(action).path == SAVE_PATH:
            candidate = form
            break
    if candidate is None:
        raise ValueError("找不到公差申請的儲存表單；未送出。")
    hidden = candidate["hidden"]
    assert isinstance(hidden, dict)
    if (not hidden.get("_token") or hidden.get("_method") != "put"
            or hidden.get("status") != "create"
            or hidden.get("myFunction") != "businessApplyForm"):
        raise ValueError("表單防偽或狀態欄位不符預期；未送出。")

    apply_info = _read_json(opener, "getApplyInfo", user_agent)
    if (not isinstance(apply_info, dict) or not apply_info.get("PersonalNoEncode")
            or apply_info.get("ParttimeAgent")):
        raise ValueError("無法確認填單人；未送出。")
    applicant_id = str(apply_info["PersonalNoEncode"])
    html_filer = str(hidden.get("ApplyPersonalEncode") or "")
    html_applicant = str(hidden.get("PersonalNoEncode") or "")
    if html_filer and html_filer.strip().lower() != applicant_id.strip().lower():
        raise ValueError("表單填單人與 API 回傳不一致；未送出。")
    if html_applicant and html_applicant.strip().lower() != applicant_id.strip().lower():
        raise ValueError("表單申請人與填單人不同；未送出。")
    query = urllib.parse.urlencode({
        "personalNoEncode": applicant_id,
        "attendTypeNo": "BusinessApply",
    })
    user_response = _read_json(opener, "getUserInfo?" + query, user_agent)
    if (not isinstance(user_response, dict) or user_response.get("msg")
            or not isinstance(user_response.get("userInfo"), list)
            or len(user_response["userInfo"]) != 1):
        raise ValueError("申請人 API 回傳不符預期；未送出。")
    user = user_response["userInfo"][0]
    if (not isinstance(user, dict) or user.get("PersonalNoEncode") != applicant_id
            or user.get("TypeNo") != "10400"
            or user.get("IsAtUnitRange") != "Y"
            or user.get("IsAvailableType") != "Y"
            or user.get("IsSupervisorToday") != "N"
            or user.get("IsNeedFactualMemo")):
        raise ValueError("申請人身份或權限與已驗證的教師案例不同；未送出。")

    supervisor_query = urllib.parse.urlencode({
        "StartDate": str(prepared["start_date"]),
        "EndDate": str(prepared["end_date"]),
        "PersonalNoEncode": applicant_id,
    })
    supervisor_info = _read_json(opener, "getSupervisorInfo?" + supervisor_query, user_agent)
    if supervisor_info not in (None, [], {}):
        raise ValueError("申請期間有兼主管資料，需代理人欄位；未送出。")

    zip_code = str(prepared["zip_code"])
    if f'value="{zip_code}"' not in page:
        raise ValueError("縣市代碼不在目前表單選項中；未送出。")
    fields = {str(k): str(v) for k, v in hidden.items()}
    fields.update({
        "ApplyPersonalEncode": applicant_id,
        "ApplyExt": str(apply_info.get("TelExt") or ""),
        "Cname": str(user.get("Cname") or ""),
        "PersonalNoEncode": applicant_id,
        "LawType": str(user.get("LawType") or ""),
        "TelExt": str(user.get("TelExt") or ""),
        "TypeCname": str(user.get("TypeCname") or ""),
        "TypeNo": str(user.get("TypeNo") or ""),
        "TitleCname": str(user.get("TitleCname") or ""),
        "TitleNo": str(user.get("TitleNo") or ""),
        "TaskUnitFullCname": str(user.get("continent") or user.get("UnitFullCname") or ""),
        "TaskUnitNo": str(user.get("TaskUnitNo") or ""),
        "SubTaskUnitNo": str(user.get("SubTaskUnitNo") or ""),
        "StartDate": str(prepared["start_date"]),
        "StartTimeHour": "8",
        "StartTimeMinute": "0",
        "EndDate": str(prepared["end_date"]),
        "EndTimeHour": "12",
        "EndTimeMinute": "0",
        "IsIncludeHoliday": "N",
        "ApplyHours": "4",
        "Zip[0]": zip_code,
        "OrganPlace": str(prepared["location"]),
        "WorkPlace": str(user.get("WorkPlace") or ""),
        "Reason": str(prepared["reason"]),
        "NoFileDescription": "",
        "delConfirmFileNfs": "",
        "AgentCname": "",
        "AgentPersonalEncode": "",
        "AgentTaskUnitFullCname": "",
        "AgentTaskUnitNo": "",
        "AgentTypeCname": "",
        "AgentTypeNo": "",
        "AgentTitleCname": "",
        "AgentTitleNo": "",
        "PostApplyDesc": "",
        "delPostApplyFileNfs": "",
        "Memo": "",
    })
    if any(not fields[key] for key in (
            "Cname", "PersonalNoEncode", "LawType", "TypeNo", "TitleNo",
            "TaskUnitNo", "WorkPlace", "Reason")):
        raise ValueError("申請人或必填欄位不完整；未送出。")
    attachments = prepared["attachment_paths"]
    assert isinstance(attachments, list)
    body, content_type = _multipart(fields, attachments)
    return urllib.request.Request(
        "https://cis.ncu.edu.tw" + SAVE_PATH,
        data=body,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
            "Content-Type": content_type,
            "Origin": "https://cis.ncu.edu.tw",
            "Referer": create_url,
        },
        method="POST",
    )


def save_draft(opener, create_html: bytes, create_url: str,
               prepared: dict[str, object], user_agent: str) -> tuple[int, str]:
    """Send at most one POST; return final HTTP status and URL, not proof of save."""
    request = build_draft_request(opener, create_html, create_url, prepared, user_agent)
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as exc:
        # urllib raises for HTTP 4xx/5xx. Never retry automatically: the server
        # might already have written a record before returning an error.
        return exc.code, exc.geturl()
    response.read(2_000_000)
    return response.getcode(), response.geturl()
