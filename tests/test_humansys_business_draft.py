"""Offline checks: never contact HumanSys or send an application."""

# Keep draft request checks isolated from live server operations.

from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from humansys_business_draft import build_draft_request


PERSON_ID = "0x" + "a" * 32


class FakeResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.url = url
        self.body = body

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, request, timeout: int):  # type: ignore[no-untyped-def]
        self.assert_get(request)
        url = request.full_url
        self.calls.append(urlsplit(url).path)
        if urlsplit(url).path.endswith("/getApplyInfo"):
            body = ('{"PersonalNoEncode":"' + PERSON_ID + '","TelExt":"12345"}').encode()
        elif urlsplit(url).path.endswith("/getUserInfo"):
            body = ('{"msg":"","userInfo":[{'
                    '"PersonalNoEncode":"' + PERSON_ID + '",'
                    '"Cname":"測試人員","LawType":"TEA-001",'
                    '"TypeNo":"10400","TypeCname":"教師(專任)",'
                    '"TitleNo":"7039","TitleCname":"副教授",'
                    '"TelExt":"12345","TaskUnitNo":"T400",'
                    '"SubTaskUnitNo":"T430","continent":"測試單位",'
                    '"WorkPlace":"校本部","IsAtUnitRange":"Y",'
                    '"IsAvailableType":"Y","IsSupervisorToday":"N",'
                    '"IsNeedFactualMemo":""}]}').encode()
        elif urlsplit(url).path.endswith("/getSupervisorInfo"):
            body = b"[]"
        else:
            raise AssertionError(f"Unexpected URL: {urlsplit(url).path}")
        return FakeResponse(url, body)

    @staticmethod
    def assert_get(request) -> None:  # type: ignore[no-untyped-def]
        if request.get_method() != "GET":
            raise AssertionError("Preflight unexpectedly sent a POST")


class DraftBuildTests(unittest.TestCase):
    def test_empty_html_identity_is_filled_from_api_without_post(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            attachment = Path(folder) / "proof.png"
            attachment.write_bytes(b"\x89PNG\r\n\x1a\n")
            prepared = {
                "start_date": "1151028", "end_date": "1151028",
                "start_time": "08:00", "end_time": "12:00",
                "elapsed_hours": 4, "include_holiday": "N",
                "zip_code": "407", "location": "測試地點",
                "reason": "測試事由", "attachment_paths": [attachment],
            }
            page = b'''<form action="update" method="post">
                <input type="hidden" name="_token" value="test-token">
                <input type="hidden" name="_method" value="put">
                <input type="hidden" name="status" value="create">
                <input type="hidden" name="myFunction" value="businessApplyForm">
                <input type="hidden" name="ApplyPersonalEncode" value="">
                <input type="hidden" name="PersonalNoEncode" value="">
                <option value="407">407</option></form>'''
            opener = FakeOpener()
            request = build_draft_request(
                opener, page,
                "https://cis.ncu.edu.tw/HumanSys/personal/personalAttendApply/businessApply/create",
                prepared, "Test User Agent",
            )
            self.assertEqual(len(opener.calls), 3)
            self.assertEqual(request.get_method(), "POST")
            self.assertIn(b'4', request.data)
            self.assertIn(b'ConfirmFileNfs[]', request.data)


if __name__ == "__main__":
    unittest.main()
