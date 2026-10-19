"""Guarded direct save for NCU personnel-fee reimbursement.

An isolated Chrome instance runs the official category, payee, and SUM_ALERT
handlers. The browser cannot send the final save POST. This module verifies the
captured official form and returns a PreparedSave for the existing one-shot
HTTP sender. It never sends a reimbursement during preparation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import tempfile
import urllib.parse

from account_direct_save import PreparedSave, SAVE_FIELDS
from account_session_probe import COOKIE_NAME_PATTERN, cookie_jar


CAPTURE_SCRIPT = Path(__file__).parent / "browser" / "account_personnel_capture.mjs"
PERSONNEL_PLAN = "11421012"
PERSONNEL_CATEGORY = "15"
PERSONNEL_NAME = "人事費用報支"
PAYEE_NAME = "詳如清冊"


def validate_capture(data: object, record: dict[str, str],
                     original_cookies: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (save fields, browser cookies) only for the exact intended case."""
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
        raise ValueError("官方人事費表單格式不正確；沒有存入。")
    fields: dict[str, str] = {}
    for pair in data["fields"]:
        if (not isinstance(pair, list) or len(pair) != 2 or
                not all(isinstance(part, str) for part in pair) or pair[0] in fields):
            raise ValueError("官方儲存欄位重複或格式不正確；沒有存入。")
        fields[pair[0]] = pair[1]
    if set(fields) != set(SAVE_FIELDS):
        raise ValueError("官方儲存欄位與已核對版本不同；沒有存入。")
    main = data.get("main")
    payee = data.get("payee")
    if not isinstance(main, dict) or not isinstance(payee, dict):
        raise ValueError("官方經費或受款人資料缺失；沒有存入。")
    if (record.get("budgetNo") != PERSONNEL_PLAN or
            main.get("category") != PERSONNEL_CATEGORY or
            main.get("plan") != PERSONNEL_PLAN or
            main.get("code") != "300" or
            main.get("amount") != record.get("amount") or
            main.get("move") != "6"):
        raise ValueError("官方人事費經費選擇與本機資料不一致；沒有存入。")
    expected_fields = {
        "SAMPLENO": "", "APPU": "", "IsNew": "Y",
        "ISAPPY": "True", "ISVOUC": "True",
        "ISTEMP": "False", "ISCASH": "False", "ISPURC": "False",
        "APPYSET": PERSONNEL_CATEGORY, "MOVETYPE": "6",
        "NO_MATH": "N", "APPYBUY": "", "ALL_DEPT": "",
        "APYNO": "", "APYADD": "", "ABSTRACT": record["abstract"],
        "D_STR": f"1#{PERSONNEL_PLAN}#300#{record['amount']}###6@",
        "P_STR": "",
    }
    for name, expected in expected_fields.items():
        if fields[name] != expected:
            raise ValueError(f"官方人事費欄位 {name} 與預期不符；沒有存入。")
    if fields["SETNAME"].strip() != PERSONNEL_NAME:
        raise ValueError("官方報支類別名稱不符；沒有存入。")
    if (not re.fullmatch(r"[A-Za-z0-9]{5,20}", fields["APYUSERNO"]) or
            not fields["APYDEP"] or not fields["ADATE"] or
            not re.fullmatch(r"\d{2,3}", fields["APPYYEAR"])):
        raise ValueError("官方申請人、年度、日期或單位缺失；沒有存入。")
    if (payee.get("name") != PAYEE_NAME or
            payee.get("amount") != record["payeeAmount"] or
            payee.get("hasVendorId") is not True):
        raise ValueError("官方詳如清冊受款人或金額未正確帶入；沒有存入。")
    a_str = fields["A_STR"]
    if a_str.count("@") != 1 or not a_str.endswith("@"):
        raise ValueError("官方受款人明細筆數不正確；沒有存入。")
    parts = a_str[:-1].split("#")
    if (len(parts) != 13 or parts[0] != "1" or not parts[1] or
            parts[2] != PAYEE_NAME or parts[5] != record["payeeAmount"] or
            parts[-1] != ""):
        raise ValueError("官方詳如清冊明細格式或金額不正確；沒有存入。")
    browser_cookies = data.get("cookies")
    if (not isinstance(browser_cookies, dict) or not browser_cookies or
            not set(browser_cookies).intersection(original_cookies) or
            any(not isinstance(name, str) or not COOKIE_NAME_PATTERN.fullmatch(name) or
                not isinstance(value, str) or not value or len(value) > 8192
                for name, value in browser_cookies.items())):
        raise ValueError("官方瀏覽器 Session 不完整；沒有存入。")
    return fields, browser_cookies


def prepare_personnel_save(record: dict[str, str], cookies: dict[str, str],
                           context: ssl.SSLContext, script: str) -> PreparedSave:
    """Prepare only; the caller reserves a one-shot attempt before final POST."""
    if record.get("budgetNo") != PERSONNEL_PLAN:
        raise ValueError("人事費本機直存目前只支援已核對的計畫 11421012；沒有存入。")
    if not re.fullmatch(r"[0-9]{1,10}", record.get("amount", "")) or int(record["amount"]) <= 0:
        raise ValueError("人事費金額不正確；沒有存入。")
    if not cookies or any(not COOKIE_NAME_PATTERN.fullmatch(name) for name in cookies):
        raise ValueError("請先更新報帳 Session；沒有存入。")
    try:
        with tempfile.TemporaryDirectory(prefix="ncu-personnel-") as profile:
            payload = json.dumps({"script": script, "cookies": cookies,
                                  "profile": profile}, ensure_ascii=False)
            process = subprocess.Popen(
                ["node", str(CAPTURE_SCRIPT)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, _stderr = process.communicate(
                    input=payload.encode("utf-8"), timeout=120,
                )
            except subprocess.TimeoutExpired as exc:
                # Chrome inherits the isolated process group. Kill both before
                # TemporaryDirectory removes the cookie-bearing profile.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise ValueError("本機 Chrome 準備官方人事費表單逾時；沒有存入。") from exc
            exit_code = process.returncode
    except OSError as exc:
        raise ValueError("本機 Chrome 無法完成官方人事費表單準備；沒有存入。") from exc
    if exit_code != 0 or len(stdout) > 100_000:
        # Chrome stderr and page contents may contain private data: never surface them.
        raise ValueError("官方人事費表單未完成或已變更；沒有存入。請確認報帳 Session 有效。")
    try:
        captured = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("官方人事費表單擷取結果無法辨識；沒有存入。") from exc
    fields, browser_cookies = validate_capture(captured, record, cookies)
    try:
        body = urllib.parse.urlencode(
            [(name, fields[name]) for name in SAVE_FIELDS], encoding="cp950",
            errors="strict", quote_via=urllib.parse.quote_plus,
        ).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("人事費欄位含報帳系統不支援字元；沒有存入。") from exc
    return PreparedSave(cookie_jar(browser_cookies), context, body,
                        record["budgetNo"], record["amount"], True)
