"""The sample application in ``examples/sample_app`` against a real FillerAI.

It is an outside application, so it is started the way one would be: its own
server, talking to FillerAI's ``/v1`` over HTTP with a token. These tests
check the parts a host has to get right - the token stays on the server, the
record on file is the server's to say, and a submit goes through the
application's own rules - rather than the chat, which ``test_bot.py`` covers.
"""

from __future__ import annotations

import importlib.util
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

from test_rest import RestCase

_spec = importlib.util.spec_from_file_location("sample_app", ROOT / "examples/sample_app/app.py")
sample_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sample_app)

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
        status, saved = self.app_json("/api/address", {"values": effect["values"]})
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
        status, body = self.app_json("/api/address", {"values": {
            "street_address": "1 Main St", "city": "Irvine", "state": "CA",
            "postal_code": "9261"}})
        self.assertEqual(status, 400)
        self.assertIn("ZIP", body["error"])
        status, body = self.app_json("/api/documents", {"values": {
            "document_type": "ID card", "policy_number": "PA-0000001"}})
        self.assertEqual(status, 403, "a policy that is not on the account")
        status, body = self.app_json("/api/documents", {"values": {"document_type": "id card"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["reference"].startswith("DOC-"))

    def test_the_page_gets_the_client_through_the_application(self):
        status, text = self.app_call("/fillerai.js")
        self.assertEqual(status, 200)
        self.assertIn(b"class ChatWidget", text)
        status, text = self.app_call("/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), text, "the token never reaches the page")
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
            # FillerAI here was not started with --bot-transcribe, and says so.
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read())["code"], "transcription_off")
        finally:
            speaking.shutdown()
            speaking.server_close()
            thread.join(timeout=5)

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
