"""Tests for the research personnel A-form."""

import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from research_human_a import (HUMAN_URL, SUMMARY_URL, fetch_human_people,
                              save_human_a_pdf)


SUMMARY = b'''<html><a href="https://cis.ncu.edu.tw/ResearchSys/budget/budgetHuman?budgetId=18175">
    \xe5\x88\x97\xe5\x8d\xb0</a></html>'''
DETAIL = '''<html><form method="get" action="https://cis.ncu.edu.tw/ResearchSys/budget/budgetHuman"
    target="_blank"><input name="action" type="hidden" value="print">
    <input name="budgetId" type="hidden" value="18175">
    <select id="printType" name="printType"><option value="a_type">甲式</option></select>
    <input id="salaryStartDate" name="salaryStartDate">
    <input id="salaryEndDate" name="salaryEndDate">
    <input id="printList" type="submit" name="Submit" value="列印">
    <div id="other"><table><tr><td><input type="checkbox" class="keyList" name="keyList[]"
    value="BH_43323"></td><td>計畫主持人</td><td>單位</td><td>教授</td><td>測試人員</td></tr></table></div>
    <div id="staff"><input type="checkbox" class="keyList" name="keyList[]"
    value="BH_45628"></div></form></html>'''.encode()
PDF = b"%PDF-1.7\n" + b"a" * 200 + b"\n%%EOF"


class HumanTypeATests(unittest.TestCase):
    def test_people_only_include_official_other_tab(self):
        with patch("research_human_a._get", side_effect=[
            (SUMMARY, "text/html"), (DETAIL, "text/html")
        ]) as get:
            people = fetch_human_people(object(), "18175")
        self.assertEqual([(person.key, person.label) for person in people],
                         [("BH_43323", "計畫主持人 · 測試人員")])
        self.assertEqual(get.call_args_list[0].args[1], SUMMARY_URL)
        self.assertEqual(get.call_args_list[1].args[1], HUMAN_URL + "?budgetId=18175")

    def test_print_uses_official_get_and_exact_filename(self):
        with tempfile.TemporaryDirectory() as dirname:
            with patch("research_human_a._get", side_effect=[
                (SUMMARY, "text/html"), (DETAIL, "text/html"),
                (PDF, "application/pdf"),
            ]) as get:
                path = save_human_a_pdf(object(), "18175", "BH_43323",
                                        "11511", "11511", Path(dirname))
            self.assertEqual(path.name, "甲式.pdf")
            self.assertEqual(path.read_bytes(), PDF)
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(get.call_args_list[2].args[1]).query)
            self.assertEqual(query, {
                "action": ["print"], "budgetId": ["18175"],
                "printType": ["a_type"], "salaryStartDate": ["11511"],
                "salaryEndDate": ["11511"], "Submit": ["列印"],
                "keyList[]": ["BH_43323"],
            })
            with patch("research_human_a._get") as get:
                with self.assertRaisesRegex(ValueError, "已存在"):
                    save_human_a_pdf(object(), "18175", "BH_43323",
                                     "11511", "11511", Path(dirname))
            get.assert_not_called()

    def test_invalid_person_or_month_never_prints(self):
        with tempfile.TemporaryDirectory() as dirname:
            for start, end in (("11513", "11513"), ("11512", "11511")):
                with self.subTest(start=start, end=end):
                    with patch("research_human_a._get") as get:
                        with self.assertRaisesRegex(ValueError, "起迄年月"):
                            save_human_a_pdf(object(), "18175", "BH_43323",
                                             start, end, Path(dirname))
                        get.assert_not_called()
            with patch("research_human_a._get", side_effect=[
                (SUMMARY, "text/html"), (DETAIL, "text/html"),
            ]) as get:
                with self.assertRaisesRegex(ValueError, "沒有選取的人員"):
                    save_human_a_pdf(object(), "18175", "BH_45628",
                                     "11511", "11511", Path(dirname))
                self.assertEqual(get.call_count, 2)

    def test_non_pdf_response_is_not_saved(self):
        with tempfile.TemporaryDirectory() as dirname:
            with patch("research_human_a._get", side_effect=[
                (SUMMARY, "text/html"), (DETAIL, "text/html"),
                (b"<html>error</html>", "text/html"),
            ]):
                with self.assertRaisesRegex(ValueError, "不是完整 PDF"):
                    save_human_a_pdf(object(), "18175", "BH_43323",
                                     "11511", "11511", Path(dirname))
            self.assertFalse((Path(dirname) / "甲式.pdf").exists())


if __name__ == "__main__":
    unittest.main()
