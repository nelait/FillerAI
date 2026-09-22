"""The ``/v1`` integration API, over a real socket.

The point of this surface is that it is *not* the UI's: a different
credential, a different lifetime, a different set of promises. So these
tests are mostly about the differences - that a cookie buys nothing here,
that a model is addressed by its library id and survives a restart, that a
token pinned to one model cannot reach past it - and only then about the
predictions coming back in the right shape.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.auth import ADMIN
from fillerai.generate.dataset import Options, generate
from fillerai.schema import Constraints, Field, FormSchema, Option
from fillerai.tokens import Tokens
from fillerai.train.model import TrainOptions, train
from fillerai.web import rest
from fillerai.web import server as server_module
from fillerai.web.server import Handler, create_server


def _json(raw: bytes):
    """A decoded body, or the bytes. A 204 preflight has neither."""
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def a_form() -> FormSchema:
    """A small form with real structure: a rule, a habit and a free field.

    Deliberately not an example off disk. The shape is what the assertions
    are about - one field the model can derive, one it will guess, one it
    must decline - and reading that off a 40-field example would make every
    failure here a puzzle.
    """
    return FormSchema(
        name="policy_intake",
        fields=[
            Field(name="first_name", label="First name", semantic_type="first_name"),
            Field(name="last_name", label="Last name", semantic_type="last_name"),
            Field(name="country", label="Country", semantic_type="country",
                  control="select",
                  options=[Option(value="US"), Option(value="CA")]),
            Field(name="currency", label="Currency", semantic_type="currency",
                  control="select",
                  options=[Option(value="USD"), Option(value="CAD")]),
            Field(name="email", label="Email", semantic_type="email",
                  constraints=Constraints(required=True)),
        ],
    )


def a_model():
    schema = a_form()
    records = generate(schema, Options(count=240, seed=7)).records
    # The relationship the model is meant to find: currency follows country.
    for record in records:
        record["currency"] = "USD" if record.get("country") == "US" else "CAD"
        record["country"] = record.get("country") or "US"
    return train(schema, records, TrainOptions(seed=7)), records


class RestCase(unittest.TestCase):
    """A server with accounts, one user, one saved model and a token."""

    password = "a-long-enough-password"

    @classmethod
    def setUpClass(cls):
        Handler.quiet = True
        cls._was = (server_module.DATABASE, server_module.AUTH,
                    server_module.CORS_ALLOW)
        server_module.open_database("sqlite://:memory:", accounts=True)
        cls.auth = server_module.AUTH
        cls.user = cls.auth.create_user("krishna", cls.password, role=ADMIN)
        cls.other = cls.auth.create_user("someone", cls.password)

        from fillerai.dbstore import DatabaseStore

        cls.store = DatabaseStore(server_module.DATABASE, owner=cls.user.id)
        model, records = a_model()
        schema_entry = cls.store.save_schema(model.schema)
        dataset = cls.store.save_dataset(records, schema_entry.id)
        cls.model_id = cls.store.save_model(model, dataset.id, "policy model").id
        cls.second_id = cls.store.save_model(model, dataset.id, "a spare").id

        cls.tokens = Tokens(server_module.DATABASE)
        cls.token = cls.tokens.issue(cls.user.id, "tests")[1]
        cls.pinned = cls.tokens.issue(cls.user.id, "pinned",
                                      model_id=cls.model_id)[1]
        cls.stranger = cls.tokens.issue(cls.other.id, "stranger")[1]

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
        (server_module.DATABASE, server_module.AUTH,
         server_module.CORS_ALLOW) = cls._was

    # -- helpers --------------------------------------------------------

    def call(self, method, path, payload=None, token="", headers=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, _json(response.read()), response.headers
        except urllib.error.HTTPError as error:
            return error.code, _json(error.read()), error.headers

    def get(self, path, token=None, **kwargs):
        return self.call("GET", path, None,
                         self.token if token is None else token, **kwargs)

    def post(self, path, payload=None, token=None, **kwargs):
        return self.call("POST", path, payload or {},
                         self.token if token is None else token, **kwargs)


class TestTheDoor(RestCase):
    def test_health_answers_without_a_token(self):
        status, body, _ = self.get("/v1/health", token="")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "fillerai")
        self.assertEqual(body["version"], fillerai.__version__)
        self.assertEqual(body["api"], "v1")

    def test_everything_else_needs_one(self):
        for method, path in (("GET", "/v1/models"),
                             ("GET", f"/v1/models/{self.model_id}"),
                             ("POST", f"/v1/models/{self.model_id}/suggest"),
                             ("POST", f"/v1/models/{self.model_id}/fill"),
                             ("POST", f"/v1/models/{self.model_id}/batch")):
            with self.subTest(path):
                status, body, _ = self.call(method, path,
                                            {} if method == "POST" else None, "")
                self.assertEqual(status, 401)
                self.assertEqual(body["code"], "no_token")

    def test_a_bad_token_is_refused_without_saying_why(self):
        for bad in ("nonsense", "flr_aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbb",
                    self.token + "x"):
            with self.subTest(bad[:20]):
                status, body, _ = self.get("/v1/models", token=bad)
                self.assertEqual(status, 401)
                self.assertEqual(body["error"], "that API token is not valid")

    def test_the_scheme_has_to_be_bearer(self):
        status, body, _ = self.call(
            "GET", "/v1/models", None, "",
            headers={"Authorization": f"Token {self.token}"})
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "no_token")

    def test_a_session_cookie_buys_nothing_here(self):
        """The whole reason this surface has its own credential.

        A browser signed in to the UI has a cookie the browser attaches by
        itself. If ``/v1`` honoured it, any page open in that browser could
        read the person's library.
        """
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(
            self.base + "/api/auth/login",
            data=json.dumps({"username": "krishna",
                             "password": self.password}).encode(),
            headers={"Content-Type": "application/json"})
        with opener.open(request) as response:
            self.assertEqual(response.status, 200)
        self.assertTrue([c for c in jar])

        try:
            with opener.open(self.base + "/v1/models") as response:
                self.fail(f"the cookie was accepted: {response.status}")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)
            self.assertEqual(json.loads(error.read())["code"], "no_token")

    def test_a_disabled_account_s_token_stops_working(self):
        disposable = self.auth.create_user("temporary", self.password)
        secret = self.tokens.issue(disposable.id, "temp")[1]
        self.assertEqual(self.get("/v1/models", token=secret)[0], 200)
        self.auth.set_active(disposable.id, False)
        status, body, _ = self.get("/v1/models", token=secret)
        self.assertEqual(status, 401)

    def test_using_a_token_stamps_it(self):
        record, secret = self.tokens.issue(self.user.id, "stamped")
        self.assertIsNone(record.last_used)
        self.get("/v1/models", token=secret)
        self.assertTrue(self.tokens.get(record.id).last_used)


class TestListingAndDescribing(RestCase):
    def test_the_listing_names_the_form_each_model_is_for(self):
        status, body, _ = self.get("/v1/models")
        self.assertEqual(status, 200)
        found = {m["id"]: m for m in body["models"]}
        self.assertIn(self.model_id, found)
        self.assertEqual(found[self.model_id]["form"], "policy_intake")
        self.assertEqual(found[self.model_id]["name"], "policy model")
        self.assertGreater(found[self.model_id]["trained_on"], 0)

    def test_one_library_is_never_another_s(self):
        status, body, _ = self.get("/v1/models", token=self.stranger)
        self.assertEqual((status, body["count"]), (200, 0))
        status, body, _ = self.get(f"/v1/models/{self.model_id}",
                                   token=self.stranger)
        self.assertEqual(status, 404)

    def test_the_model_card_carries_what_a_form_needs_to_draw_itself(self):
        status, body, _ = self.get(f"/v1/models/{self.model_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["model"]["form"], "policy_intake")
        self.assertTrue(body["ask_first"])
        fields = {f["name"]: f for f in body["fields"]}
        self.assertEqual(fields["country"]["label"], "Country")
        self.assertEqual([o["value"] for o in fields["country"]["options"]],
                         ["US", "CA"])
        self.assertTrue(fields["email"]["required"])
        self.assertEqual(fields["email"]["options"], [])
        # The report and the schema both, joined here so no client has to.
        self.assertIn(fields["currency"]["how"], ("rule", "learned", "usual"))

    def test_asking_for_something_that_is_not_a_model(self):
        schema_entry = [e for e in self.store.list(kind="schema")][0]
        status, body, _ = self.get(f"/v1/models/{schema_entry.id}")
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "not_a_model")

    def test_asking_for_a_model_that_is_not_there(self):
        status, body, _ = self.get("/v1/models/mdl-nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_a_pinned_token_reaches_its_model_and_nothing_else(self):
        self.assertEqual(self.get(f"/v1/models/{self.model_id}",
                                  token=self.pinned)[0], 200)
        status, body, _ = self.get(f"/v1/models/{self.second_id}",
                                   token=self.pinned)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "out_of_scope")
        # And it sees only that one in the listing, rather than a list it
        # would be refused on every row of.
        _status, listing, _ = self.get("/v1/models", token=self.pinned)
        self.assertEqual([m["id"] for m in listing["models"]], [self.model_id])


class TestSuggesting(RestCase):
    def test_a_learned_relationship_comes_back_with_its_reason(self):
        status, body, _ = self.post(
            f"/v1/models/{self.model_id}/suggest", {"observed": {"country": "CA"}})
        self.assertEqual(status, 200)
        rows = {r["field"]: r for r in body["suggestions"]}
        self.assertEqual(rows["currency"]["value"], "CAD")
        self.assertTrue(rows["currency"]["accepted"])
        self.assertTrue(rows["currency"]["because"])
        self.assertEqual(body["values"]["currency"], "CAD")
        # What was given back is never suggested again.
        self.assertNotIn("country", rows)
        self.assertEqual(body["given"], {"country": "CA"})

    def test_a_field_nothing_can_predict_is_declined_rather_than_guessed(self):
        _status, body, _ = self.post(
            f"/v1/models/{self.model_id}/suggest", {"observed": {"country": "US"}})
        rows = {r["field"]: r for r in body["suggestions"]}
        self.assertIsNone(rows["email"]["value"])
        self.assertFalse(rows["email"]["accepted"])
        self.assertNotIn("email", body["values"])

    def test_the_threshold_decides_what_is_offered_not_what_is_reported(self):
        low = self.post(f"/v1/models/{self.model_id}/suggest",
                        {"observed": {"country": "US"}, "threshold": 0.0})[1]
        high = self.post(f"/v1/models/{self.model_id}/suggest",
                         {"observed": {"country": "US"}, "threshold": 1.0})[1]
        self.assertEqual(len(low["suggestions"]), len(high["suggestions"]))
        self.assertGreater(low["offered"], high["offered"])
        self.assertEqual(high["offered"], 0)

    def test_a_caller_can_ask_about_only_the_fields_it_cares_about(self):
        _status, body, _ = self.post(
            f"/v1/models/{self.model_id}/suggest",
            {"observed": {"country": "CA"}, "fields": ["currency"]})
        self.assertEqual([r["field"] for r in body["suggestions"]], ["currency"])

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        for payload in ({"observed": {"nope": "x"}},
                        {"observed": {}, "fields": ["nope"]}):
            with self.subTest(payload):
                status, body, _ = self.post(
                    f"/v1/models/{self.model_id}/suggest", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "unknown_field")

    def test_a_threshold_outside_the_range_is_refused(self):
        for bad in (-0.1, 1.5, "soon"):
            with self.subTest(bad):
                status, body, _ = self.post(
                    f"/v1/models/{self.model_id}/suggest", {"threshold": bad})
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "bad_threshold")

    def test_observed_has_to_be_an_object(self):
        status, body, _ = self.post(f"/v1/models/{self.model_id}/suggest",
                                    {"observed": ["country"]})
        self.assertEqual((status, body["code"]), (400, "bad_observed"))


class TestFillingAndBatching(RestCase):
    def test_fill_hands_back_the_record_and_says_what_it_added(self):
        status, body, _ = self.post(f"/v1/models/{self.model_id}/fill",
                                    {"observed": {"country": "CA"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["record"]["country"], "CA")
        self.assertEqual(body["record"]["currency"], "CAD")
        self.assertIn("currency", body["filled"])
        self.assertNotIn("country", body["filled"])

    def test_batch_answers_every_record_in_order(self):
        status, body, _ = self.post(
            f"/v1/models/{self.model_id}/batch",
            {"records": [{"country": "CA"}, {"country": "US"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertEqual([r["index"] for r in body["results"]], [0, 1])
        self.assertEqual(body["results"][0]["record"]["currency"], "CAD")
        self.assertEqual(body["results"][1]["record"]["currency"], "USD")

    def test_an_empty_or_malformed_batch_is_refused(self):
        for payload in ({"records": []}, {"records": {}}, {"records": ["x"]}, {}):
            with self.subTest(payload):
                status, body, _ = self.post(
                    f"/v1/models/{self.model_id}/batch", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "bad_records")

    def test_a_batch_has_a_ceiling(self):
        status, body, _ = self.post(
            f"/v1/models/{self.model_id}/batch",
            {"records": [{"country": "US"}] * (rest.MAX_BATCH + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "too_large")


class TestTheShapeOfTheSurface(RestCase):
    def test_a_wrong_method_says_so_rather_than_404(self):
        status, body, _ = self.post("/v1/models")
        self.assertEqual(status, 405)
        self.assertEqual(body["code"], "method_not_allowed")

    def test_an_unknown_path_under_v1_is_json_not_html(self):
        status, body, headers = self.get("/v1/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(body["code"], "not_found")

    def test_a_body_that_is_not_json_is_refused_clearly(self):
        request = urllib.request.Request(
            self.base + f"/v1/models/{self.model_id}/suggest",
            data=b"{not json", method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        try:
            urllib.request.urlopen(request)
            self.fail("accepted a broken body")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(json.loads(error.read())["code"], "bad_json")

    def test_a_body_that_is_not_an_object_is_refused(self):
        request = urllib.request.Request(
            self.base + f"/v1/models/{self.model_id}/suggest",
            data=b"[1, 2]", method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        try:
            urllib.request.urlopen(request)
            self.fail("accepted a list as a body")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)

    def test_every_refusal_carries_a_code_a_program_can_branch_on(self):
        status, body, _ = self.get("/v1/models/mdl-nope")
        self.assertEqual(sorted(body), ["code", "error"])
        self.assertTrue(status >= 400)


class TestCrossOrigin(RestCase):
    def setUp(self):
        self._was = server_module.CORS_ALLOW

    def tearDown(self):
        server_module.CORS_ALLOW = self._was

    def test_with_accounts_on_any_origin_may_call_it(self):
        # Safe because the credential is a bearer token a page has to be
        # given, never a cookie the browser attaches by itself.
        server_module.CORS_ALLOW = None
        _status, _body, headers = self.get(
            "/v1/models", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIsNone(headers.get("Access-Control-Allow-Credentials"))

    def test_a_named_origin_is_echoed_and_others_are_not(self):
        server_module.CORS_ALLOW = ("http://localhost:3000",)
        _s, _b, allowed = self.get("/v1/models",
                                   headers={"Origin": "http://localhost:3000"})
        self.assertEqual(allowed["Access-Control-Allow-Origin"],
                         "http://localhost:3000")
        _s, _b, refused = self.get("/v1/models",
                                   headers={"Origin": "http://evil.example"})
        self.assertIsNone(refused.get("Access-Control-Allow-Origin"))

    def test_the_preflight_names_the_header_the_token_travels_in(self):
        server_module.CORS_ALLOW = None
        status, _body, headers = self.call(
            "OPTIONS", f"/v1/models/{self.model_id}/suggest", None, "",
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 204)
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_nothing_is_allowed_when_nothing_is_allowed(self):
        server_module.CORS_ALLOW = ()
        status, _body, headers = self.call(
            "OPTIONS", "/v1/models", None, "",
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_the_ui_s_own_endpoints_never_answer_a_preflight(self):
        server_module.CORS_ALLOW = None
        status, _body, _headers = self.call("OPTIONS", "/api/extract", None, "")
        self.assertEqual(status, 404)


class TestCachingTheModel(RestCase):
    def test_a_deleted_model_is_not_served_out_of_the_cache(self):
        from fillerai.dbstore import DatabaseStore

        store = DatabaseStore(server_module.DATABASE, owner=self.user.id)
        model, records = a_model()
        schema_entry = store.save_schema(model.schema)
        dataset = store.save_dataset(records, schema_entry.id)
        doomed = store.save_model(model, dataset.id, "doomed").id
        secret = self.tokens.issue(self.user.id, "doomed token")[1]

        self.assertEqual(self.get(f"/v1/models/{doomed}", token=secret)[0], 200)
        # Deleted through the UI's own endpoint, which is how it happens.
        signed_in = self._sign_in()
        status, _body = signed_in("/api/library/delete", {"id": doomed})
        self.assertEqual(status, 200)
        self.assertEqual(self.get(f"/v1/models/{doomed}", token=secret)[0], 404)

    def _sign_in(self):
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        csrf = {"value": ""}

        def post(path, payload):
            headers = {"Content-Type": "application/json"}
            if csrf["value"]:
                headers[server_module.CSRF_HEADER] = csrf["value"]
            request = urllib.request.Request(
                self.base + path, data=json.dumps(payload).encode(),
                headers=headers)
            with opener.open(request) as response:
                body = json.load(response)
                if body.get("csrf"):
                    csrf["value"] = body["csrf"]
                return response.status, body

        post("/api/auth/login", {"username": "krishna", "password": self.password})
        return post


class TestServedClient(RestCase):
    def test_the_javascript_client_is_served_where_the_docs_say(self):
        with urllib.request.urlopen(self.base + "/client/fillerai.js") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers["Content-Type"])

    def test_the_client_is_reachable_without_signing_in(self):
        # It is inert: everything it wraps needs a token of its own.
        request = urllib.request.Request(self.base + "/client/fillerai.js")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"class FillerAI", response.read())

    def test_traversal_out_of_the_client_directory_is_refused(self):
        for path in ("/client/../fillerai/auth.py", "/client/../../pyproject.toml"):
            with self.subTest(path):
                try:
                    urllib.request.urlopen(self.base + path)
                    self.fail(f"served {path}")
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 404)


class NoAccountsCase(unittest.TestCase):
    """The other posture: ``--no-auth``, one library, nobody to be.

    Worth its own server because the reasoning is the opposite way round.
    There is no credential to check, so the thing keeping somebody else's
    page out of the library is that the browser will not let it in - which
    is why cross-origin is off by default here and on by default when a
    token is required.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile

        from fillerai.store import Store

        Handler.quiet = True
        cls._was = (server_module.DATABASE, server_module.AUTH,
                    server_module.LIBRARY, server_module.CORS_ALLOW)
        server_module.DATABASE, server_module.AUTH = None, None
        server_module.CORS_ALLOW = None
        cls._dir = tempfile.mkdtemp(prefix="fillerai-test-rest-")
        cls._rmtree = shutil.rmtree
        server_module.LIBRARY = Store(cls._dir)
        rest.forget()

        model, records = a_model()
        schema_entry = server_module.LIBRARY.save_schema(model.schema)
        dataset = server_module.LIBRARY.save_dataset(records, schema_entry.id)
        cls.model_id = server_module.LIBRARY.save_model(
            model, dataset.id, "the only model").id

        cls.httpd = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        rest.forget()
        (server_module.DATABASE, server_module.AUTH,
         server_module.LIBRARY, server_module.CORS_ALLOW) = cls._was
        cls._rmtree(cls._dir, ignore_errors=True)

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, _json(response.read()), response.headers
        except urllib.error.HTTPError as error:
            return error.code, _json(error.read()), error.headers

    def test_the_one_library_answers_without_a_token(self):
        status, body, _ = self.get("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["models"]], [self.model_id])

    def test_no_browser_on_another_origin_may_reach_it_by_default(self):
        """With no credential, ``*`` would open the library to any page."""
        _status, _body, headers = self.get(
            "/v1/models", headers={"Origin": "http://evil.example"})
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_naming_an_origin_is_how_a_local_app_is_let_in(self):
        server_module.CORS_ALLOW = ("http://localhost:5173",)
        try:
            _s, _b, headers = self.get(
                "/v1/models", headers={"Origin": "http://localhost:5173"})
            self.assertEqual(headers["Access-Control-Allow-Origin"],
                             "http://localhost:5173")
        finally:
            server_module.CORS_ALLOW = None

    def test_tokens_cannot_be_issued_with_nowhere_to_keep_them(self):
        request = urllib.request.Request(
            self.base + "/api/tokens", data=b"{}",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request)
            self.fail("issued a token with no database")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 409)


class TestTheTokenEndpoints(RestCase):
    """The UI's own way in to the same tokens, which is a cookie surface."""

    def setUp(self):
        import http.cookiejar

        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.ui("/api/auth/login",
                {"username": "krishna", "password": self.password})

    def ui(self, path, payload=None):
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers[server_module.CSRF_HEADER] = self.csrf
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload or {}).encode(),
            headers=headers)
        try:
            with self.opener.open(request) as response:
                status, body = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, body = error.code, json.loads(error.read())
        if isinstance(body, dict) and body.get("csrf"):
            self.csrf = body["csrf"]
        return status, body

    def test_a_token_issued_in_the_ui_works_against_the_service(self):
        status, body = self.ui("/api/tokens/create", {"name": "from the UI"})
        self.assertEqual(status, 200)
        self.assertTrue(body["secret"].startswith("flr_"))
        self.assertEqual(self.get("/v1/models", token=body["secret"])[0], 200)

    def test_the_listing_never_carries_a_secret_back(self):
        _status, made = self.ui("/api/tokens/create", {"name": "secretive"})
        _status, listed = self.ui("/api/tokens")
        self.assertNotIn(made["secret"], json.dumps(listed))
        self.assertTrue(any(t["name"] == "secretive" for t in listed["tokens"]))

    def test_revoking_through_the_ui_stops_the_service_call(self):
        _status, made = self.ui("/api/tokens/create", {"name": "doomed"})
        self.assertEqual(self.get("/v1/models", token=made["secret"])[0], 200)
        status, _body = self.ui("/api/tokens/revoke", {"id": made["token"]["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/v1/models", token=made["secret"])[0], 401)

    def test_somebody_else_s_token_is_not_yours_to_revoke(self):
        mine = self.tokens.issue(self.other.id, "not yours")[0]
        # This account is an administrator, so make an ordinary one to ask.
        plain = self.auth.create_user("plain", self.password)
        secret_ui = TestTheTokenEndpoints("test_a_token_issued_in_the_ui_works_"
                                          "against_the_service")
        secret_ui.base = self.base
        import http.cookiejar

        secret_ui.jar = http.cookiejar.CookieJar()
        secret_ui.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(secret_ui.jar))
        secret_ui.csrf = ""
        secret_ui.ui("/api/auth/login",
                     {"username": plain.username, "password": self.password})
        status, body = secret_ui.ui("/api/tokens/revoke", {"id": mine.id})
        self.assertEqual(status, 403)
        self.assertIn("somebody else", body["error"])

    def test_an_expiry_has_to_be_a_sensible_number_of_days(self):
        for bad in ("soon", 0, 99999):
            with self.subTest(bad):
                status, _body = self.ui("/api/tokens/create", {"days": bad})
                self.assertEqual(status, 400)

    def test_a_token_can_be_pinned_to_a_model_from_the_ui(self):
        _status, body = self.ui("/api/tokens/create",
                                {"name": "pinned", "model_id": self.model_id})
        self.assertEqual(body["token"]["model_id"], self.model_id)
        secret = body["secret"]
        self.assertEqual(self.get(f"/v1/models/{self.second_id}",
                                  token=secret)[0], 403)

    def test_pinning_to_something_that_is_not_there_is_refused(self):
        status, _body = self.ui("/api/tokens/create", {"model_id": "mdl-nope"})
        self.assertEqual(status, 404)

    def test_the_endpoints_need_a_session_like_every_other_ui_endpoint(self):
        request = urllib.request.Request(
            self.base + "/api/tokens", data=b"{}",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request)
            self.fail("listed tokens signed out")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)


if __name__ == "__main__":
    unittest.main()
