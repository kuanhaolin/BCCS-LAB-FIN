"""Tests for research budget listing and printing."""

import http.cookiejar
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_budget import (LIST_URL, Project, bootstrap_research_from_portal, budget_print_target,
                             cookies_from_header, fetch_projects, parse_projects,
                             save_budget_pdf)
from ncu_form_app import research_list_page, research_page


HTML = b'''<html><title>\xe5\x80\x8b\xe4\xba\xba\xe7\xb6\x93\xe5\x88\x86\xe8\xa1\xa8\xe7\xae\xa1\xe7\x90\x86</title>
<table><tr><th>\xe7\xb6\x93\xe8\xb2\xbb\xe6\xb5\x81\xe6\xb0\xb4\xe8\x99\x9f</th></tr></table>
<table><tr><th>\xe8\xa8\x88\xe7\x95\xab\xe6\xb5\x81\xe6\xb0\xb4\xe8\x99\x9f</th><th>\xe6\x9c\x9f\xe5\x88\xa5</th>
<th>\xe8\xa8\x88\xe7\x95\xab\xe5\x90\x8d\xe7\xa8\xb1</th><th>\xe8\xa8\x88\xe7\x95\xab\xe4\xb8\xbb\xe6\x8c\x81\xe4\xba\xba</th>
<th>\xe5\x9f\xb7\xe8\xa1\x8c\xe5\x96\xae\xe4\xbd\x8d</th><th>\xe5\xa7\x94\xe8\xa8\x97\xe5\x96\xae\xe4\xbd\x8d</th>
<th>\xe8\xa8\x88\xe7\x95\xab\xe8\xb5\xb7\xe6\x97\xa5</th><th>\xe8\xa8\x88\xe7\x95\xab\xe8\xbf\x84\xe6\x97\xa5</th><th></th></tr>
<tr><td>11516004</td><td>1</td><td>\xe9\x9b\xb2\xe7\xab\xaf\xe5\x84\xb2\xe5\xad\x98<br><span>[\xe6\x9c\xaa\xe6\x8f\x90\xe6\x92\xa5]</span></td>
<td>\xe7\x94\xb2</td><td>\xe8\xb3\x87\xe7\xae\xa1\xe7\xb3\xbb</td><td>\xe5\x9c\x8b\xe7\xb6\xb2</td><td>1150415</td><td>1161130</td>
<td><a href="budgetInfo/23129">\xe7\xb6\x93\xe8\xb2\xbb\xe7\x8f\xbe\xe6\xb3\x81\xe6\x9f\xa5\xe8\xa9\xa2</a>
<form method="get" action="budgetPrint"><input type="hidden" name="BudgetInfoId" value="23129"></form></td></tr>
</table></html>'''
PDF = b"%PDF-1.7\n" + b"x" * 120 + b"\n%%EOF\n"


class ResearchBudgetTests(unittest.TestCase):
    def test_portal_sso_builds_in_memory_research_session(self):
        row = parse_projects(HTML, "text/html")[0]

        def respond(opener, url, user_agent, **kwargs):
            self.assertEqual(url, LIST_URL)
            jar = next(handler.cookiejar for handler in opener.handlers
                       if isinstance(handler, urllib.request.HTTPCookieProcessor))
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="laravel_session", value="research-only",
                port=None, port_specified=False, domain="cis.ncu.edu.tw",
                domain_specified=False, domain_initial_dot=False,
                path="/ResearchSys", path_specified=True, secure=True,
                expires=None, discard=True, comment=None, comment_url=None,
                rest={}, rfc2109=False,
            ))
            return SimpleNamespace(
                read=lambda limit: HTML, geturl=lambda: LIST_URL,
                headers={"Content-Type": "text/html; charset=utf-8"},
                getcode=lambda: 200,
            )

        with patch("research_budget.open_page", side_effect=respond), \
             patch("research_budget.research_opener", return_value=object()) as build, \
             patch("research_budget.fetch_projects", return_value=[row]) as fetch:
            opener, rows = bootstrap_research_from_portal(
                "portal=a; SESSION=b; XSRF-TOKEN=c")
        self.assertEqual(rows, [row])
        self.assertIsNotNone(opener)
        self.assertEqual(build.call_args.args[0]["laravel_session"], "research-only")
        fetch.assert_called_once_with(opener)

    def test_expired_or_login_portal_cannot_connect_research(self):
        for path in ("expired", "login"):
            response = SimpleNamespace(
                read=lambda limit: b"<html>login</html>",
                geturl=lambda: "https://portal.ncu.edu.tw/" + path,
                headers={"Content-Type": "text/html"},
                getcode=lambda: 200,
            )
            with self.subTest(path=path), patch("research_budget.open_page", return_value=response):
                with self.assertRaisesRegex(ValueError, "未登入或已過期"):
                    bootstrap_research_from_portal("portal=a; SESSION=b; XSRF-TOKEN=c")

    def test_cookie_requires_laravel_session_and_no_duplicates(self):
        self.assertEqual(cookies_from_header("laravel_session=abc; XSRF-TOKEN=def"),
                         {"laravel_session": "abc", "XSRF-TOKEN": "def"})
        for raw in ("XSRF-TOKEN=def", "laravel_session=a; laravel_session=b",
                    "laravel_session=a\nCookie: x"):
            with self.assertRaises(ValueError):
                cookies_from_header(raw)

    def test_parse_project_and_print_id(self):
        rows = parse_projects(HTML, "text/html; charset=utf-8")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].budget_id, "23129")
        self.assertEqual(rows[0].number, "11516004")
        self.assertEqual(rows[0].period, "1")
        self.assertEqual(rows[0].name, "雲端儲存 [未提撥]")

    def test_rejects_login_or_wrong_print_action(self):
        with self.assertRaisesRegex(ValueError, "未辨識"):
            parse_projects(b"<html>login</html>", "text/html")
        changed = HTML.replace(b'action="budgetPrint"', b'action="https://evil.invalid/print"')
        self.assertEqual(parse_projects(changed, "text/html"), [])

    def test_fetch_uses_official_filter(self):
        with patch("research_budget._get", return_value=(HTML, "text/html")) as get:
            self.assertEqual(fetch_projects(object(), "Y")[0].budget_id, "23129")
            get.assert_called_once_with(unittest.mock.ANY, LIST_URL + "?IsClose=Y")

    def test_print_saves_pdf_once_and_never_overwrites(self):
        row = parse_projects(HTML, "text/html")[0]
        with tempfile.TemporaryDirectory() as directory, \
             patch("research_budget._get", return_value=(PDF, "application/pdf")) as get:
            folder = Path(directory) / "prints"
            saved = save_budget_pdf(object(), row, folder)
            self.assertEqual(saved.name, "經分表_11516004_第1期.pdf")
            self.assertEqual(saved.read_bytes(), PDF)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "已存在"):
                save_budget_pdf(object(), row, folder)
            self.assertIn("BudgetInfoId=23129", get.call_args.args[1])

    def test_html_print_requires_matching_project_and_known_title(self):
        row = parse_projects(HTML, "text/html")[0]
        landing = (b'<html><a href="https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint/23129?action=pdf">'
                   + '列印經分表'.encode() + b'</a></html>')
        print_html = '<html><title>經分表</title><body>計畫 11516004 異動歷程</body></html>'.encode()
        with tempfile.TemporaryDirectory() as directory, \
             patch("research_budget._get", side_effect=[(landing, "text/html"),
                                                         (print_html, "text/html")]) as get, \
             patch("research_budget._cookies", return_value=[{
                 "name": "laravel_session", "value": "x", "path": "/ResearchSys"}]), \
             patch("research_budget.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=PDF, stderr=b"")) as render:
            saved = save_budget_pdf(object(), row, Path(directory))
            self.assertEqual(saved.read_bytes(), PDF)
            self.assertEqual(render.call_count, 1)
            self.assertEqual(render.call_args.kwargs["timeout"], 90)
            self.assertEqual(get.call_args.args[1],
                             "https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint/23129?action=pdf")
        with patch("research_budget._get", return_value=(b"<html><title>Login</title></html>", "text/html")), \
             patch("research_budget.subprocess.run") as render:
            with self.assertRaisesRegex(ValueError, "唯一"):
                save_budget_pdf(object(), row)
            render.assert_not_called()

    def test_print_link_must_be_unique_and_belong_to_same_budget(self):
        valid = ('<a class="btn btn-success btn-sm" '
                 'href="https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint/23129?action=pdf" '
                 'target="_blank" rel="noopener noreferrer">'
                 '<i class="bi bi-printer-fill"></i><span>列印經分表</span></a>').encode()
        self.assertEqual(budget_print_target(valid, "23129"),
                         "https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint/23129?action=pdf")
        for bad in (valid.replace(b"23129", b"99999"),
                    valid.replace(b"cis.ncu.edu.tw", b"evil.invalid"),
                    valid.replace(b"action=pdf", b"action=html")):
            with self.assertRaisesRegex(ValueError, "唯一"):
                budget_print_target(bad, "23129")

    def test_intermediate_page_then_exact_pdf_link(self):
        row = parse_projects(HTML, "text/html")[0]
        landing = ('<html><a href="/ResearchSys/budget/budgetPrint/23129?action=pdf">'
                   '列印經分表</a></html>').encode()
        with tempfile.TemporaryDirectory() as directory, \
             patch("research_budget._get", side_effect=[(landing, "text/html"),
                                                         (PDF, "application/pdf")]) as get:
            saved = save_budget_pdf(object(), row, Path(directory))
            self.assertEqual(saved.read_bytes(), PDF)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.args[1],
                             "https://cis.ncu.edu.tw/ResearchSys/budget/budgetPrint/23129?action=pdf")

    def test_list_page_has_one_print_button_and_escaped_values(self):
        row = Project("23129", "11516004", "1", "<test>", "甲", "資管系", "國網", "1150415", "1161130")
        output = research_list_page(SimpleNamespace(csrf="token"), [row], "").decode()
        self.assertIn("列印經分表", output)
        self.assertIn("&lt;test&gt;", output)
        self.assertNotIn("<test>", output)
        self.assertIn("主要包含 ① portal ② SESSION ③ XSRF-TOKEN。", research_page().decode())
        self.assertIn('action="/research/connect"', research_page().decode())
        self.assertIn('>列出計劃</button>', research_page().decode())
        self.assertNotIn('重新讀取計畫', research_page().decode())
        self.assertNotIn("researchCookie", research_page().decode())


if __name__ == "__main__":
    unittest.main()
