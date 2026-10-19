"""Save one verified accounting print page as a private local PDF."""

from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import urllib.parse
import urllib.request

from account_direct_save import USER_AGENT
from account_purchase_list import PurchaseRow, purchase_print_url
from account_session_probe import HOST, SameHostRedirect, cookie_jar


PRINT_DIR = Path(__file__).with_name("prints")
RENDERER = Path(__file__).parent / "browser" / "account_render_pdf.mjs"
MAX_PRINT_BYTES = 10 * 1024 * 1024


def find_purchase(rows: list[PurchaseRow], number: str) -> PurchaseRow:
    if not re.fullmatch(r"\d{1,20}", number):
        raise ValueError("請輸入正確請購單號；沒有產生 PDF。")
    matches = [row for row in rows if row.number == number]
    if len(matches) != 1:
        raise ValueError("未審核清單中找不到唯一的指定單號；沒有產生 PDF。")
    return matches[0]


def fetch_print_page(cookies: dict[str, str], context: ssl.SSLContext,
                     url: str) -> tuple[bytes, str]:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar(cookies)),
        urllib.request.HTTPSHandler(context=context), SameHostRedirect(),
    )
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,text/html,application/xhtml+xml",
        "Referer": "https://account.ncu.edu.tw/APSWIS_Q/Dept_Add_Q/DA_SerSTART_Q.asp",
    })
    with opener.open(request, timeout=30) as response:
        actual = urllib.parse.urlsplit(response.geturl())
        expected_path = urllib.parse.urlsplit(url).path
        if (response.status != 200 or actual.scheme != "https"
                or actual.hostname != HOST or actual.path != expected_path
                or not re.fullmatch(r"/APSWIS_Q/TMP_PAG_Q/NCU\d{1,3}\.asp", actual.path)):
            raise ValueError("官方列印頁未正常載入；沒有產生 PDF。")
        content_type = response.headers.get("Content-Type", "").lower()
        body = response.read(MAX_PRINT_BYTES + 1)
    if len(body) > MAX_PRINT_BYTES or not body:
        raise ValueError("官方列印頁為空或過大；沒有產生 PDF。")
    if body.startswith(b"%PDF-"):
        return body, "pdf"
    if ("html" in content_type and
            (b"<html" in body[:4096].lower() or b"<!doctype" in body[:4096].lower())):
        return body, "html"
    raise ValueError("官方列印頁不是可辨識的 PDF 或 HTML；沒有產生 PDF。")


def render_html_pdf(url: str, cookies: dict[str, str], number: str) -> bytes:
    payload = json.dumps({"url": url, "cookies": cookies, "number": number})
    try:
        result = subprocess.run(
            ["node", str(RENDERER)], input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("本機 Chrome 無法完成 PDF 列印；沒有保存檔案。") from exc
    if result.returncode != 0:
        # The renderer's error output is deliberately never forwarded: it may
        # include private page content or a URL.
        raise ValueError("官方列印頁無法轉成 PDF；沒有保存檔案。")
    return result.stdout


def _valid_pdf(data: bytes) -> bool:
    return (len(data) > 100 and len(data) <= MAX_PRINT_BYTES
            and data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:])


def save_purchase_pdf(row: PurchaseRow, roc_year: int, cookies: dict[str, str],
                      context: ssl.SSLContext, folder: Path = PRINT_DIR) -> Path:
    url = purchase_print_url(row, roc_year)
    body, kind = fetch_print_page(cookies, context, url)
    pdf = body if kind == "pdf" else render_html_pdf(url, cookies, row.number)
    if not _valid_pdf(pdf):
        raise ValueError("列印結果不是完整 PDF；沒有保存檔案。")
    if folder.is_symlink():
        raise ValueError("PDF 資料夾不可為符號連結；沒有保存檔案。")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"憑證單_{row.number}.pdf"
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
