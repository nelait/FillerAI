"""The web API with accounts turned on, over a real socket.

``test_web`` runs the same server without accounts, which is the other half
of the promise: the same endpoints, and what changes is who is let through
and whose library they land in. So these cover the door rather than the
stages - signing in, the token, the two roles, and the wall between one
person's library and another's.
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.auth import ADMIN
from fillerai.web import server as server_module
from fillerai.web.server import CSRF_HEADER, Handler, create_server


class Client:
    """One browser: its own cookie jar, and the token that goes with it."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.token = ""

    def post(self, path: str, payload=None, token: bool = True):
        headers = {"Content-Type": "application/json"}
        if token and self.token:
            headers[CSRF_HEADER] = self.token
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload or {}).encode(),
            headers=headers)
        try:
            with self.opener.open(request) as response:
                status, body = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, body = error.code, json.load(error)
        if isinstance(body, dict) and body.get("csrf"):
            self.token = body["csrf"]
        return status, body

    def get(self, path: str, follow: bool = True):
        opener = self.opener if follow else urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())
        try:
            with opener.open(self.base + path) as response:
                return response.status, response.geturl(), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Location", path), error.read()

    def sign_in(self, username: str, password: str):
        return self.post("/api/auth/login",
                         {"username": username, "password": password})

    @property
    def cookies(self):
        return {cookie.name: cookie for cookie in self.jar}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AuthServerCase(unittest.TestCase):
    """A server with accounts, one administrator and one ordinary user."""

    admin_password = "an-administrators-password"
    user_password = "an-ordinary-password"

    @classmethod
    def setUpClass(cls):
        Handler.quiet = True
        cls._was = (server_module.DATABASE, server_module.AUTH)
        server_module.open_database("sqlite://:memory:", accounts=True)
        cls.auth = server_module.AUTH
        cls.admin = cls.auth.create_user("root", cls.admin_password, role=ADMIN)
        cls.user = cls.auth.create_user("krishna", cls.user_password)

        cls.httpd = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server_module.close_database()
        server_module.DATABASE, server_module.AUTH = cls._was

    def client(self) -> Client:
        return Client(self.base)

    def signed_in(self, username: str = "root", password: str | None = None) -> Client:
        client = self.client()
        status, _body = client.sign_in(
            username,
            password if password is not None
            else (self.admin_password if username == "root" else self.user_password))
        self.assertEqual(status, 200)
        return client


class TestTheDoor(AuthServerCase):
    def test_the_app_sends_a_signed_out_browser_to_the_login_page(self):
        status, where, _body = self.client().get("/", follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(where, "/login")

    def test_the_login_page_and_its_assets_are_served_signed_out(self):
        for path in ("/login", "/static/styles.css", "/static/login.js"):
            with self.subTest(path):
                status, _where, body = self.client().get(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_a_signed_in_browser_gets_the_app_and_not_the_login_page(self):
        client = self.signed_in()
        status, _where, body = client.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"panel-source", body)
        status, where, _body = client.get("/login", follow=False)
        self.assertEqual((status, where), (302, "/"))

    def test_every_stage_is_refused_without_a_session(self):
        for path in ("/api/extract", "/api/generate", "/api/train", "/api/library",
                     "/api/predict", "/api/simulate/form"):
            with self.subTest(path):
                status, body = self.client().post(path, {})
                self.assertEqual(status, 401)
                self.assertTrue(body["sign_in"])

    def test_meta_answers_signed_out_but_says_nothing_else(self):
        status, body = self.client().post("/api/meta")
        self.assertEqual(status, 200)
        self.assertTrue(body["accounts"])
        self.assertFalse(body["signed_in"])
        self.assertNotIn("examples", body)
        self.assertNotIn("library", body)

    def test_meta_signed_in_carries_the_user_and_the_token(self):
        _status, body = self.signed_in().post("/api/meta")
        self.assertEqual(body["user"]["username"], "root")
        self.assertTrue(body["csrf"])
        self.assertIn("algorithms", body)


class TestSigningIn(AuthServerCase):
    def test_a_wrong_password_is_refused_and_sets_no_cookie(self):
        client = self.client()
        status, _body = client.sign_in("root", "not-the-password")
        self.assertEqual(status, 401)
        self.assertNotIn("fillerai_session", client.cookies)

    def test_an_unknown_user_reads_the_same_as_a_wrong_password(self):
        _status, missing = self.client().sign_in("nobody-here", "whatever-it-is")
        _status, wrong = self.client().sign_in("root", "not-the-password")
        self.assertEqual(missing["error"], wrong["error"])

    def test_signing_in_sets_a_cookie_no_script_can_read(self):
        client = self.signed_in()
        cookie = client.cookies["fillerai_session"]
        self.assertTrue(cookie.value)
        self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))

    def test_signing_out_ends_the_session_for_good(self):
        client = self.signed_in("krishna")
        self.assertEqual(client.post("/api/auth/logout")[0], 200)
        self.assertEqual(client.post("/api/library")[0], 401)

    def test_who_am_i(self):
        _status, body = self.signed_in("krishna").post("/api/auth/me")
        self.assertEqual(body["user"]["username"], "krishna")
        self.assertFalse(body["admin"])


class TestTheToken(AuthServerCase):
    def test_a_call_without_the_token_is_refused(self):
        client = self.signed_in()
        status, body = client.post("/api/library", {}, token=False)
        self.assertEqual(status, 403)
        self.assertTrue(body["stale"])

    def test_a_call_with_somebody_elses_token_is_refused(self):
        mine = self.signed_in()
        theirs = self.signed_in("krishna")
        mine.token = theirs.token
        self.assertEqual(mine.post("/api/library")[0], 403)

    def test_the_login_itself_needs_no_token(self):
        client = self.client()
        status, _body = client.post(
            "/api/auth/login",
            {"username": "root", "password": self.admin_password}, token=False)
        self.assertEqual(status, 200)


class TestRoles(AuthServerCase):
    def test_an_ordinary_user_cannot_reach_the_admin_endpoints(self):
        client = self.signed_in("krishna")
        for path in ("/api/admin/users", "/api/admin/users/create",
                     "/api/admin/users/update", "/api/admin/users/delete",
                     "/api/admin/database", "/api/admin/import"):
            with self.subTest(path):
                status, body = client.post(path, {})
                self.assertEqual(status, 403)
                self.assertIn("administrator", body["error"])

    def test_an_administrator_sees_everybody(self):
        status, body = self.signed_in().post("/api/admin/users")
        self.assertEqual(status, 200)
        names = {user["username"] for user in body["users"]}
        self.assertIn("root", names)
        self.assertIn("krishna", names)
        self.assertEqual(body["you"], self.admin.id)

    def test_an_ordinary_user_can_still_do_the_work(self):
        client = self.signed_in("krishna")
        status, body = client.post("/api/extract",
                                   {"kind": "html",
                                    "content": "<form><input name=email></form>"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["schema"]["fields"]), 1)


class TestManagingUsers(AuthServerCase):
    def setUp(self):
        self.admin_client = self.signed_in()

    def made(self, username: str, **extra):
        status, body = self.admin_client.post(
            "/api/admin/users/create", dict({"username": username}, **extra))
        self.assertEqual(status, 200, body)
        self.addCleanup(self._remove, body["user"]["id"])
        return body

    def _remove(self, user_id: str):
        try:
            self.auth.delete_user(user_id)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass

    def test_a_new_account_gets_a_password_shown_once(self):
        body = self.made("newcomer")
        self.assertTrue(body["password"])
        self.assertTrue(body["user"]["must_change"])
        # And it works.
        client = self.client()
        self.assertEqual(client.sign_in("newcomer", body["password"])[0], 200)

    def test_a_password_can_be_given_instead_of_generated(self):
        body = self.made("chosen", password="a-password-they-chose")
        self.assertIsNone(body["password"])
        self.assertFalse(body["user"]["must_change"])

    def test_a_bad_username_is_refused_with_the_rule(self):
        status, body = self.admin_client.post("/api/admin/users/create",
                                              {"username": "Not A Name"})
        self.assertEqual(status, 400)
        self.assertIn("lowercase", body["error"])

    def test_a_name_already_taken_is_refused(self):
        status, body = self.admin_client.post("/api/admin/users/create",
                                              {"username": "root"})
        self.assertEqual(status, 409)

    def test_a_role_can_be_changed_both_ways(self):
        made = self.made("promotable")
        user_id = made["user"]["id"]
        _status, body = self.admin_client.post("/api/admin/users/update",
                                               {"id": user_id, "role": "admin"})
        self.assertEqual(body["user"]["role"], "admin")
        _status, body = self.admin_client.post("/api/admin/users/update",
                                               {"id": user_id, "role": "user"})
        self.assertEqual(body["user"]["role"], "user")

    def test_disabling_an_account_shuts_the_door_at_once(self):
        made = self.made("temporary")
        client = self.client()
        client.sign_in("temporary", made["password"])
        self.admin_client.post("/api/admin/users/update",
                               {"id": made["user"]["id"], "active": False})
        self.assertEqual(client.post("/api/auth/me")[0], 401)
        self.assertEqual(self.client().sign_in("temporary", made["password"])[0], 403)

    def test_a_reset_password_must_be_changed_before_anything_else(self):
        made = self.made("resettable", password="the-first-password")
        _status, reset = self.admin_client.post("/api/admin/users/password",
                                                {"id": made["user"]["id"]})
        client = self.client()
        status, body = client.sign_in("resettable", reset["password"])
        self.assertEqual(status, 200)
        self.assertTrue(body["must_change"])

        status, body = client.post("/api/library")
        self.assertEqual(status, 403)
        self.assertTrue(body["must_change"])

        status, _body = client.post("/api/auth/password",
                                    {"current": reset["password"],
                                     "new": "a-password-of-their-own"})
        self.assertEqual(status, 200)
        self.assertEqual(client.post("/api/library")[0], 200)

    def test_changing_your_own_password_needs_the_current_one(self):
        client = self.signed_in("krishna")
        status, body = client.post("/api/auth/password",
                                   {"current": "not-it", "new": "something-else"})
        self.assertEqual(status, 403)
        self.assertIn("current password", body["error"])

    def test_changing_it_keeps_you_signed_in_here_and_nowhere_else(self):
        self.made("changer", password="the-first-password")
        here = self.client()
        here.sign_in("changer", "the-first-password")
        elsewhere = self.client()
        elsewhere.sign_in("changer", "the-first-password")

        status, _body = here.post("/api/auth/password",
                                  {"current": "the-first-password",
                                   "new": "the-second-password"})
        self.assertEqual(status, 200)
        self.assertEqual(here.post("/api/library")[0], 200)
        self.assertEqual(elsewhere.post("/api/library")[0], 401)

    def test_an_account_can_be_deleted_but_not_your_own(self):
        made = self.made("departing")
        status, body = self.admin_client.post("/api/admin/users/delete",
                                              {"id": self.admin.id})
        self.assertEqual(status, 400)
        self.assertIn("signed in as", body["error"])

        status, body = self.admin_client.post("/api/admin/users/delete",
                                              {"id": made["user"]["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["username"], "departing")

    def test_the_last_administrator_cannot_be_taken_away(self):
        """There is one here, so every way of losing them is refused."""
        for change in ({"role": "user"}, {"active": False}):
            with self.subTest(change):
                status, body = self.admin_client.post(
                    "/api/admin/users/update", dict({"id": self.admin.id}, **change))
                self.assertEqual(status, 400)
                self.assertIn("only administrator", body["error"])


class TestSeparateLibraries(AuthServerCase):
    def test_what_one_person_makes_the_other_cannot_see(self):
        mine = self.signed_in()
        theirs = self.signed_in("krishna")

        mine.post("/api/extract", {"kind": "html",
                                   "content": "<form><input name=email></form>"})
        _status, my_library = mine.post("/api/library")
        self.assertGreater(len(my_library["entries"]), 0)

        _status, their_library = theirs.post("/api/library")
        self.assertEqual(their_library["entries"], [])

        stranger = my_library["entries"][0]["id"]
        for path in ("/api/library/open", "/api/library/export",
                     "/api/library/delete", "/api/library/rename"):
            with self.subTest(path):
                status, _body = theirs.post(path, {"id": stranger, "name": "mine now"})
                self.assertEqual(status, 404)

    def test_a_deleted_user_takes_their_library_with_them(self):
        _status, made = self.signed_in().post("/api/admin/users/create",
                                              {"username": "briefly"})
        self.addCleanup(self._forget, made["user"]["id"])
        client = self.client()
        client.sign_in("briefly", made["password"])
        client.post("/api/auth/password",
                    {"current": made["password"], "new": "a-password-of-their-own"})
        client.post("/api/extract", {"kind": "html",
                                     "content": "<form><input name=zip></form>"})

        admin_client = self.signed_in()
        _status, listing = admin_client.post("/api/admin/users")
        theirs = next(u for u in listing["users"] if u["username"] == "briefly")
        self.assertGreater(theirs["entries"], 0)

        status, _body = admin_client.post("/api/admin/users/delete",
                                          {"id": theirs["id"]})
        self.assertEqual(status, 200)
        _status, listing = admin_client.post("/api/admin/users")
        self.assertNotIn("briefly", [u["username"] for u in listing["users"]])
        # And the entries went with them, rather than sitting in the database
        # under an owner nobody can sign in as.
        _status, facts = admin_client.post("/api/admin/database")
        self.assertEqual(facts["entries"], 0)

    def _forget(self, user_id: str) -> None:
        try:
            self.auth.delete_user(user_id)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass


class TestTheDatabasePanel(AuthServerCase):
    def test_it_says_where_the_data_is(self):
        _status, body = self.signed_in().post("/api/admin/database")
        self.assertEqual(body["database"]["backend"], "SQLiteDatabase")
        self.assertIn("users", body["database"]["tables"])
        self.assertGreaterEqual(body["database"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
