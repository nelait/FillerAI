"""The documentation site, its access code, and the in-app help.

The renderer is checked on the constructs the documents use, the catalog on
what is in ``docs/``, and the door over a real socket: closed until an
administrator sets a code, open to a browser that gives it, and closed again
to that browser the moment the code changes.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.store import Store
from fillerai.web import docs, server as server_module
from fillerai.web.server import DOCS_COOKIE

from test_web import ServerCase
from test_web_auth import AuthServerCase


class TestRenderer(unittest.TestCase):
    def html(self, text: str) -> str:
        return docs.render(text)[0]

    def test_headings_get_the_anchors_github_gives_them(self):
        out = self.html("## 4. The model, and why\n")
        self.assertIn('<h2 id="4-the-model-and-why">', out)

    def test_repeated_headings_get_distinct_anchors(self):
        out = self.html("## Notes\n\n## Notes\n")
        self.assertIn('id="notes"', out)
        self.assertIn('id="notes-1"', out)

    def test_everything_is_escaped(self):
        out = self.html("A <script>alert(1)</script> & `<b>` here\n\n"
                        "```\n<img src=x onerror=alert(1)>\n```\n")
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<code>&lt;b&gt;</code>", out)

    def test_a_link_cannot_carry_a_script(self):
        for href in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x"):
            out = self.html(f"[x]({href})\n")
            self.assertNotIn("href", out, href)

    def test_lists_nest_and_number(self):
        out = self.html("1. one\n2. two\n   - inner\n3. three\n")
        self.assertIn("<ol>", out)
        self.assertIn("<ul><li>inner</li></ul>", out)
        self.assertEqual(out.count("<li>"), 4)

    def test_tables_render_with_alignment(self):
        out = self.html("| a | b |\n|---|--:|\n| 1 | `x|y` |\n")
        self.assertIn("<th>a</th>", out)
        self.assertIn('<td style="text-align:right">', out)

    def test_emphasis_and_code_do_not_interfere(self):
        out = self.html("**bold** and *it* and `a*b*c`\n")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>it</em>", out)
        self.assertIn("<code>a*b*c</code>", out)

    def test_mermaid_is_shown_as_its_source(self):
        out = self.html("```mermaid\ngraph LR\n  A-->B\n```\n")
        self.assertIn('class="diagram"', out)
        self.assertIn("A--&gt;B", out)

    def test_links_between_documents_become_site_links(self):
        catalog = docs.catalog()
        if "overview" not in catalog:
            self.skipTest("no docs folder in this install")
        doc = catalog["reference/cli"]
        out = docs.render("[a](../architecture.md#the-library) [b](http-api.md) "
                          "[c](../../README.md) [d](../../fillerai/cli.py)", doc)[0]
        self.assertIn('href="/docs/architecture#the-library"', out)
        self.assertIn('href="/docs/reference/http-api"', out)
        self.assertIn('href="/docs/tour"', out)
        self.assertIn('<span class="src-ref">d</span>', out)


class TestCatalog(unittest.TestCase):
    def test_the_user_guide_is_always_there(self):
        catalog = docs.catalog()
        self.assertIn("user-guide", catalog)
        groups = [name for name, _about, _docs in docs.grouped(catalog)]
        self.assertEqual(groups[0], "Help")

    def test_every_document_renders(self):
        catalog = docs.catalog()
        for slug, doc in catalog.items():
            with self.subTest(slug):
                page = docs.doc_page(catalog, doc)
                self.assertIn("<article", page)

    def test_the_help_panel_has_a_section_for_every_screen(self):
        sections = docs.help_sections()
        for panel in ("source", "schema", "generate", "train", "simulate",
                      "library", "bots", "account"):
            with self.subTest(panel):
                self.assertIn(panel, sections)
                self.assertTrue(sections[panel]["title"])
                self.assertIn("<ol>", sections[panel]["html"])

    def test_the_panel_markers_do_not_show_in_the_guide(self):
        catalog = docs.catalog()
        page = docs.doc_page(catalog, catalog["user-guide"])
        self.assertNotIn("panel:", page)


class TestDocsWithAccounts(AuthServerCase):
    def setUp(self):
        server_module._DOCS_FAILURES.clear()
        server_module.DATABASE.remember(server_module.DOCS_SETTING, "")

    def set_code(self, code: str = "open-sesame"):
        admin = self.signed_in()
        status, body = admin.post("/api/admin/docs/passcode", {"code": code})
        self.assertEqual(status, 200, body)
        return body

    def test_the_docs_are_closed_until_a_code_is_set(self):
        status, _where, body = self.client().get("/docs")
        self.assertEqual(status, 403)
        self.assertIn(b"no access code yet", body)
        status, body = self.client().post("/api/docs/unlock", {"code": "anything"})
        self.assertEqual(status, 403)

    def test_only_an_administrator_sets_the_code(self):
        user = self.signed_in("krishna")
        status, _body = user.post("/api/admin/docs/passcode", {"code": "open-sesame"})
        self.assertEqual(status, 403)
        status, _body = self.client().post("/api/admin/docs/passcode", {"code": "x" * 10})
        self.assertEqual(status, 401)

    def test_the_code_is_stored_hashed(self):
        self.set_code("open-sesame")
        stored = server_module.DATABASE.setting(server_module.DOCS_SETTING)
        self.assertNotIn("open-sesame", stored)
        self.assertIn("scrypt", stored)

    def test_a_short_code_is_refused(self):
        admin = self.signed_in()
        status, body = admin.post("/api/admin/docs/passcode", {"code": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("at least", body["error"])

    def test_the_right_code_opens_the_docs_and_the_wrong_one_does_not(self):
        self.set_code("open-sesame")
        reader = self.client()
        status, _where, body = reader.get("/docs")
        self.assertEqual(status, 200)
        self.assertIn(b'id="gate"', body)

        status, body = reader.post("/api/docs/unlock", {"code": "wrong-code"})
        self.assertEqual(status, 403)
        self.assertNotIn(DOCS_COOKIE, reader.cookies)

        status, body = reader.post("/api/docs/unlock", {"code": "open-sesame"})
        self.assertEqual(status, 200, body)
        self.assertIn(DOCS_COOKIE, reader.cookies)
        self.assertEqual(reader.cookies[DOCS_COOKIE].path, "/docs")

        status, _where, body = reader.get("/docs")
        self.assertEqual(status, 200)
        self.assertIn(b"Documentation", body)
        self.assertIn(b"/docs/user-guide", body)
        status, _where, body = reader.get("/docs/user-guide")
        self.assertEqual(status, 200)
        self.assertIn(b"User guide", body)
        status, _where, _body = reader.get("/docs/no-such-thing")
        self.assertEqual(status, 404)

    def test_the_docs_need_no_account(self):
        self.set_code("open-sesame")
        reader = self.client()
        reader.post("/api/docs/unlock", {"code": "open-sesame"})
        self.assertNotIn("fillerai_session", reader.cookies)
        status, _where, _body = reader.get("/docs/user-guide")
        self.assertEqual(status, 200)

    def test_changing_the_code_closes_the_docs_to_old_browsers(self):
        self.set_code("open-sesame")
        reader = self.client()
        reader.post("/api/docs/unlock", {"code": "open-sesame"})
        self.set_code("a-brand-new-code")
        status, _where, body = reader.get("/docs/user-guide")
        self.assertIn(b'id="gate"', body)

    def test_a_forged_cookie_is_not_enough(self):
        self.set_code("open-sesame")
        reader = self.client()
        import http.cookiejar
        reader.jar.set_cookie(http.cookiejar.Cookie(
            0, DOCS_COOKIE, "0" * 64, None, False, "127.0.0.1", False, False,
            "/docs", True, False, None, False, None, None, {}))
        _status, _where, body = reader.get("/docs/user-guide")
        self.assertIn(b'id="gate"', body)

    def test_guessing_is_throttled(self):
        self.set_code("open-sesame")
        reader = self.client()
        for _ in range(server_module.DOCS_MAX_FAILURES):
            status, _body = reader.post("/api/docs/unlock", {"code": "guess-guess"})
            self.assertEqual(status, 403)
        status, body = reader.post("/api/docs/unlock", {"code": "open-sesame"})
        self.assertEqual(status, 429, body)

    def test_a_generated_code_is_shown_once_and_works(self):
        admin = self.signed_in()
        status, body = admin.post("/api/admin/docs/passcode", {"generate": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["configured"])
        code = body["code"]
        status, body = admin.post("/api/admin/docs", {})
        self.assertNotIn("code", body)
        self.assertEqual(body["set_by"], "root")
        status, _body = self.client().post("/api/docs/unlock", {"code": code})
        self.assertEqual(status, 200)

    def test_clearing_the_code_closes_the_docs(self):
        self.set_code("open-sesame")
        admin = self.signed_in()
        status, body = admin.post("/api/admin/docs/passcode", {"clear": True})
        self.assertFalse(body["configured"])
        status, _where, _body = self.client().get("/docs")
        self.assertEqual(status, 403)

    def test_the_help_panel_needs_a_session(self):
        status, _body = self.client().post("/api/help", {})
        self.assertEqual(status, 401)
        status, body = self.signed_in("krishna").post("/api/help", {})
        self.assertEqual(status, 200)
        self.assertIn("generate", body["sections"])


class TestDocsWithoutAccounts(ServerCase):
    def setUp(self):
        server_module._DOCS_FAILURES.clear()
        (Path(self.library_dir) / server_module.DOCS_FILE).unlink(missing_ok=True)

    def test_the_code_is_kept_in_the_library_folder(self):
        status, body = self.post("/api/admin/docs/passcode", {"code": "local-code"})
        self.assertEqual(status, 200, body)
        stored = (Path(self.library_dir) / server_module.DOCS_FILE).read_text()
        self.assertNotIn("local-code", stored)
        status, body = self.post("/api/docs/unlock", {"code": "local-code"})
        self.assertEqual(status, 200)

    def test_the_help_panel_answers(self):
        status, body = self.post("/api/help", {})
        self.assertEqual(status, 200)
        self.assertIn("Generate", body["sections"]["generate"]["title"])


if __name__ == "__main__":
    unittest.main()
