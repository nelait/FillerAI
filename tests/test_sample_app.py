"""The sample application in ``fillerai/sampleapp`` against a real AIrForms.

It is an outside application, so it is started the way one would be: its own
server, talking to AIrForms's ``/v1`` over HTTP with a token. These tests
check the parts a host has to get right - the token stays on the server, the
record on file is the server's to say, the forms are the bot service's
templates, and a submit goes through the application's own rules - rather
than the chat, which ``test_bot.py`` covers.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fillerai.bot import template as templates
from fillerai.sampleapp import app as sample_app
from fillerai.tokens import Tokens
from fillerai.web import server as server_module

from test_rest import RestCase

THE_ASK = "please update my city from SFO to Irvine and house no 1429 Silverstein"


class TestSampleApp(RestCase):
    def setUp(self):
        for key in ("address_change", "document_request"):
            body = templates.starters()[key].to_dict()
            self.assertEqual(self.post("/v1/templates", {"template": body})[0], 200)
        self.dir = Path(tempfile.mkdtemp(prefix="fillerai-sample-app-"))
        self.portal = sample_app.Portal(self.dir / "portal.json")
        self.app = sample_app.make_server(sample_app.FillerAIService(self.base, self.token),
                                          self.portal, port=0)
        self.app.handler.quiet = True
        self.app_base = f"http://127.0.0.1:{self.app.server_address[1]}"
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)

    def app_call(self, path, body=None):
        request = urllib.request.Request(
            self.app_base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def app_json(self, path, body=None):
        status, raw = self.app_call(path, body)
        return status, json.loads(raw)

    def chat(self, input_, state=None, context=None):
        return self.app_json("/api/chat", {"input": input_, "state": state,
                                           "context": context or {}})

    def test_a_chat_turn_goes_through_with_the_record_on_file(self):
        # The page claims a different city; the application's server says
        # what is on file, so the before is San Francisco.
        status, reply = self.chat({"type": "text", "text": THE_ASK},
                                  context={"current": {"city": "Somewhere else"}})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["intent"]["template"], "address_change")
        self.assertEqual(reply["form"]["changes"]["city"],
                         {"before": "San Francisco", "after": "Irvine"})

    def test_the_chat_can_submit_through_the_application_s_own_path(self):
        _, reply = self.chat({"type": "text", "text": THE_ASK})
        _, reply = self.chat({"type": "text", "text": "CA"}, reply["state"])
        _, reply = self.chat({"type": "text", "text": "92618"}, reply["state"])
        _, reply = self.chat({"type": "action", "action": "submit"}, reply["state"])
        effect = reply["effect"]
        self.assertEqual(effect["type"], "submit")

        # What the page does with a submit effect: the application's path...
        status, saved = self.app_json("/api/submit/address_change", {"values": effect["values"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["customer"]["city"], "Irvine")
        self.assertEqual(saved["customer"]["postal_code"], "92618")
        # ...then the reference back to the chat.
        _, reply = self.chat({"type": "event", "event": "submitted",
                              "detail": {"reference": saved["reference"]}}, reply["state"])
        self.assertEqual(reply["conversation"]["status"], "done")
        self.assertIn(saved["reference"], reply["messages"][0]["text"])

        _, me = self.app_json("/api/me")
        self.assertEqual(me["requests"][-1]["reference"], saved["reference"])
        self.assertTrue((self.dir / "portal.json").exists(), "the record is kept")

    def test_the_application_s_rules_still_apply_to_what_the_chat_sends(self):
        address = {"street_address": "1 Main St", "city": "Irvine", "state": "CA",
                   "postal_code": "9261"}
        status, body = self.app_json("/api/submit/address_change", {"values": address})
        self.assertEqual(status, 400)
        self.assertIn("ZIP", body["error"])
        status, body = self.app_json("/api/submit/address_change",
                                     {"values": {**address, "postal_code": ""}})
        self.assertEqual((status, body["error"]), (400, "missing ZIP code"))
        document = {"document_type": "ID card", "full_name": "Jordan Lee"}
        status, body = self.app_json("/api/submit/document_request", {"values": {
            **document, "policy_number": "PA-0000001"}})
        self.assertEqual(status, 403, "a policy that is not on the account")
        status, body = self.app_json("/api/submit/document_request", {"values": {
            **document, "policy_number": "PA-1048822", "document_type": "a pony"}})
        self.assertEqual(status, 400)
        self.assertIn("one of Declarations page", body["error"])
        status, body = self.app_json("/api/submit/document_request", {"values": {
            **document, "policy_number": "PA-1048822", "document_type": "id card"}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["reference"].startswith("DR-"))
        # Which document is the request's, not the customer's.
        self.assertNotIn("document_type", body["customer"])
        _, me = self.app_json("/api/me")
        self.assertEqual(me["requests"][-1]["kind"], "Document request")
        self.assertIn("ID card", me["requests"][-1]["detail"])
        self.assertEqual(self.app_call("/api/submit/no_such_thing", {"values": {}})[0], 404)
        self.assertEqual(self.app_call("/api/submit/../etc", {"values": {}})[0], 404)

    def test_a_form_for_every_template_the_bot_service_has(self):
        status, body = self.app_json("/api/templates")
        self.assertEqual(status, 200, body)
        forms = {t["key"]: t for t in body["templates"]}
        self.assertEqual(set(forms), {"address_change", "document_request"})
        self.assertIn("postal_code", [f["name"] for f in forms["address_change"]["fields"]])
        self.assertEqual(forms["document_request"]["fields"][1]["options"][1], "ID card")

        # A template saved in AIrForms is a form on the next page load, and
        # can be submitted by hand like the others.
        phone = {"key": "phone_change", "name": "Phone change",
                 "description": "Change the phone number on file",
                 "examples": ["change my phone number", "new phone"],
                 "fields": [{"name": "phone", "label": "Phone", "required": True,
                             "semantic_type": "phone"}],
                 "actions": ["fill_form", "submit"]}
        self.assertEqual(self.post("/v1/templates", {"template": phone})[0], 200)
        try:
            _, body = self.app_json("/api/templates")
            self.assertIn("phone_change", [t["key"] for t in body["templates"]])
            status, saved = self.app_json("/api/submit/phone_change",
                                          {"values": {"phone": "(949) 555-0100"}})
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["customer"]["phone"], "(949) 555-0100")
            status, again = self.app_json("/api/submit/phone_change",
                                          {"values": {"phone": "(949) 555-0100"}})
            self.assertEqual((status, again["error"]), (400, "that is already what we have on file"))
            # And the chat can move to it from the middle of another one.
            _, reply = self.chat({"type": "text", "text": THE_ASK})
            _, reply = self.chat({"type": "text", "text": "change my phone number"},
                                 reply["state"])
            self.assertEqual(reply["intent"]["template"], "phone_change")
            self.assertEqual(reply["form"]["template"], "phone_change")
        finally:
            self.post("/v1/templates/phone_change/delete", {})

    def test_it_imports_nothing_from_fillerai(self):
        # It is an outside application that happens to ship in the package.
        tree = ast.parse((ROOT / "fillerai/sampleapp/app.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse(node.level or (node.module or "").startswith("fillerai"),
                                 node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("fillerai"), alias.name)

    def test_the_page_gets_the_client_through_the_application(self):
        status, text = self.app_call("/fillerai.js")
        self.assertEqual(status, 200)
        self.assertIn(b"class ChatWidget", text)
        status, text = self.app_call("/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), text, "the token never reaches the page")
        # One form open at a time; this is what shows while none is.
        self.assertIn(b'id="noneOpen"', text)
        script = self.app_call("/app.js")[1]
        self.assertIn(b"card.hidden = true", script)
        self.assertIn(b"openForm(chatOn)", script)
        self.assertEqual(self.app_call("/../app.py")[0], 404)

    def test_server_speech_goes_through_the_application_to_fillerai(self):
        self.assertFalse(self.app_json("/api/me")[1]["server_speech"])
        self.assertEqual(self.app_call("/api/transcribe", {"audio": "AA=="})[0], 404,
                         "not offered unless the application was started with it")
        speaking = sample_app.make_server(sample_app.FillerAIService(self.base, self.token),
                                          self.portal, port=0, server_speech=True)
        speaking.handler.quiet = True
        thread = threading.Thread(target=speaking.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{speaking.server_address[1]}"
            with urllib.request.urlopen(base + "/api/me") as response:
                self.assertTrue(json.loads(response.read())["server_speech"])
            request = urllib.request.Request(
                base + "/api/transcribe", method="POST",
                data=json.dumps({"audio": "AA==", "mime": "audio/webm"}).encode(),
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            # AIrForms here was not started with --bot-transcribe, and says so.
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read())["code"], "transcription_off")
        finally:
            speaking.shutdown()
            speaking.server_close()
            thread.join(timeout=5)

    def test_serve_starts_it_with_a_token_of_its_own(self):
        from fillerai.store import Store

        was = server_module.LIBRARY
        server_module.LIBRARY = Store(self.dir / "library")
        port = int(self.base.rsplit(":", 1)[1])
        started = []
        try:
            for _ in range(2):
                httpd, line = server_module.start_sample_app("127.0.0.1", port, port=0)
                self.assertIsNotNone(httpd, line)
                started.append(httpd)
                self.assertIn("as krishna", line)
            # A restart replaces the token rather than piling them up.
            named = [t for t in Tokens(server_module.DATABASE).list(self.user.id)
                     if t.name == server_module.SAMPLE_APP_TOKEN]
            self.assertEqual(len(named), 1)
            base = f"http://127.0.0.1:{started[-1].server_address[1]}"
            with urllib.request.urlopen(base + "/api/templates") as response:
                keys = {t["key"] for t in json.loads(response.read())["templates"]}
            self.assertEqual(keys, {"address_change", "document_request"})

            httpd, line = server_module.start_sample_app("127.0.0.1", port, port=0,
                                                         username="nobody")
            self.assertIsNone(httpd)
            self.assertIn("no user 'nobody'", line)
        finally:
            for httpd in started:
                httpd.shutdown()
                httpd.server_close()
            server_module.LIBRARY = was
            for token in Tokens(server_module.DATABASE).list(self.user.id):
                if token.name == server_module.SAMPLE_APP_TOKEN:
                    Tokens(server_module.DATABASE).forget(token.id)

    def test_the_ui_links_to_it_when_it_is_running(self):
        was = server_module.SAMPLE_APP_PORT, server_module.AUTH
        try:
            # As the single-user tool, where meta answers in full signed out.
            server_module.SAMPLE_APP_PORT, server_module.AUTH = 8100, None
            self.assertEqual(server_module.api_meta({})["sample_app_port"], 8100)
        finally:
            server_module.SAMPLE_APP_PORT, server_module.AUTH = was
        page = (ROOT / "fillerai/web/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="sampleAppLink"', page)

    def test_a_fillerai_that_is_down_is_a_502_with_a_reason(self):
        down = sample_app.FillerAIService("http://127.0.0.1:9", self.token, timeout=2)
        status, body = down.turn({"input": {"type": "text", "text": "hi"}})
        self.assertEqual((status, body["code"]), (502, "unreachable"))

    def test_a_bad_token_comes_back_as_fillerai_said_it(self):
        wrong = sample_app.FillerAIService(self.base, "flr_not_a_token")
        status, body = wrong.turn({"input": {"type": "text", "text": "hi"}})
        self.assertEqual(status, 401)
        self.assertIn("code", body)


if __name__ == "__main__":
    import unittest

    unittest.main()
