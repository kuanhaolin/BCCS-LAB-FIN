#!/usr/bin/env python3
"""Validate a HumanSys business-apply input without contacting the website.

This is intentionally a preview only: it does not log in, upload, save, or submit.
The actual save request and the site's ApplyHours calculation still need checking.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re


MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".doc", ".docx"}
REQUIRED_FIELDS = {
    "startDate", "endDate", "startTime", "endTime",
    "destinationDistrict", "location", "reason", "proofFile",
}


def parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必須是字串。")
    if re.fullmatch(r"\d{7}", value):
        year = int(value[:3]) + 1911
        month = int(value[3:5])
        day = int(value[5:7])
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"{field} 不是有效的民國日期。") from exc
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 請使用民國七碼（如 1151027）或 YYYY-MM-DD。") from exc


def format_roc_date(value: date) -> str:
    roc_year = value.year - 1911
    if not 1 <= roc_year <= 999:
        raise ValueError("日期不在可表示的民國年份範圍內。")
    return f"{roc_year:03d}{value.month:02d}{value.day:02d}"


def prepare(input_path: Path) -> dict[str, object]:
    try:
        source = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 JSON：{exc}") from exc
    return prepare_record(source, input_path.parent)


def prepare_record(source: object, base_dir: Path) -> dict[str, object]:
    """Validate one record using attachments relative to a chosen directory."""
    if isinstance(source, list):
        if len(source) != 1:
            raise ValueError("目前一次只能預覽一筆申請。")
        source = source[0]
    if not isinstance(source, dict):
        raise ValueError("輸入必須是一個物件，或只含一個物件的陣列。")

    keys_with_spaces = [key for key in source if isinstance(key, str) and key != key.strip()]
    if keys_with_spaces:
        raise ValueError("欄位名稱前後不可有空白：" + ", ".join(repr(key) for key in keys_with_spaces))
    missing = sorted(REQUIRED_FIELDS - source.keys())
    if missing:
        raise ValueError("缺少欄位：" + ", ".join(missing))

    start_date = parse_date(source["startDate"], "startDate")
    end_date = parse_date(source["endDate"], "endDate")
    try:
        start_time = datetime.strptime(source["startTime"], "%H:%M").time()
        end_time = datetime.strptime(source["endTime"], "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ValueError("startTime/endTime 請使用 24 小時制 HH:MM。") from exc
    start = datetime.combine(start_date, start_time)
    end = datetime.combine(end_date, end_time)
    if end <= start:
        raise ValueError("結束日期時間必須晚於開始日期時間。")

    district = source["destinationDistrict"]
    if not isinstance(district, str) or not re.fullmatch(r"\d{3}\s+.+", district.strip()):
        raise ValueError("destinationDistrict 應如 '407 臺中市 西屯區'。")
    zip_code = district.split()[0]
    location = source["location"]
    reason = source["reason"]
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location 不可空白。")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 100:
        raise ValueError("reason 必須是 1–100 個字元。")

    proof_files = source["proofFile"]
    if not isinstance(proof_files, list) or not proof_files:
        raise ValueError("proofFile 請至少提供一個附件。")
    attachments: list[tuple[str, int]] = []
    attachment_paths: list[Path] = []
    for item in proof_files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("proofFile 的每個項目都需要 filename。")
        filename = item["filename"]
        if Path(filename).name != filename or filename in (".", ".."):
            raise ValueError("filename 只能是附件目錄中的檔名。")
        file_path = base_dir / filename
        if not file_path.is_file():
            raise ValueError(f"找不到附件：{filename}")
        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(f"附件格式不支援：{filename}")
        size = file_path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件超過 4 MB：{filename}")
        attachments.append((filename, size))
        attachment_paths.append(file_path)

    include_holiday = source.get("includeHoliday")
    if include_holiday is not None and include_holiday not in ("Y", "N"):
        raise ValueError("includeHoliday 只能是 Y 或 N。")
    return {
        "start_date": format_roc_date(start_date),
        "end_date": format_roc_date(end_date),
        "start_time": start_time.strftime("%H:%M"),
        "end_time": end_time.strftime("%H:%M"),
        "elapsed_hours": (end - start).total_seconds() / 3600,
        "zip_code": zip_code,
        "district": district,
        "location": location.strip(),
        "reason": reason,
        "reason_length": len(reason),
        "include_holiday": include_holiday,
        "attachments": attachments,
        "attachment_paths": attachment_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="預覽公差申請輸入；不連線、不儲存、不送審。")
    parser.add_argument("input_json", nargs="?", type=Path,
                        default=Path(__file__).with_name("input.json"))
    args = parser.parse_args()
    try:
        data = prepare(args.input_json)
    except ValueError as exc:
        parser.error(str(exc))
    print("公差申請預覽（未送出）")
    print(f"  民國日期時間：{data['start_date']} {data['start_time']} → "
          f"{data['end_date']} {data['end_time']}")
    print(f"  原始時段：{data['elapsed_hours']:g} 小時；正式 ApplyHours 須以 HumanSys 試算為準")
    print(f"  縣市代碼：{data['zip_code']}（{data['district']}）")
    print(f"  地點：{data['location']}")
    print(f"  事由：{data['reason_length']} 個字元")
    print(f"  是否含假日時段：{data['include_holiday'] or '未指定（儲存前必須確認）'}")
    for filename, size in data["attachments"]:
        print(f"  附件：{filename}（{size} bytes）")
    print("未連線 HumanSys，也未建立申請。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
