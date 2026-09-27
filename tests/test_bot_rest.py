"""The bot service over HTTP: ``/v1/templates``, ``/v1/bot/turn`` and the UI's ``/api/bot``.

The conversation itself is tested in ``test_bot.py``; this is about the
surface - tokens and their scope, refusals with codes, the served client -
and about the UI's try-it chat running the very same turn.
"""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fillerai.bot import template as templates
from fillerai.store import Store
from fillerai.web import botrest, rest
from fillerai.web import server as server_module

from test_rest import RestCase, a_model

ADDRESS = templates.starters()["address_change"].to_dict()
DOCUMENT = templates.starters()["document_request"].to_dict()
THE_ASK = "please update my city from SFO to Irvine and house no 1429 Silverstein"


class BotRestCase(RestCase):
    def setUp(self):
        # Every test starts from the same two templates in the token owner's
        # library, and nothing else.
        templates.remove(self.store, "address_change")
        templates.remove(self.store, "document_request")
        templates.remove(self.store, "policy_intake")
        for body in (ADDRESS, DOCUMENT):
            status, reply, _ = self.post("/v1/templates", {"template": body})
            self.assertEqual(status, 200, reply)

    def turn(self, body, token=None):
        return self.post("/v1/bot/turn", body, token=token)


class TestTemplateEndpoints(BotRestCase):
    def test_listing_and_reading_a_template(self):
        status, body, _ = self.get("/v1/templates")
        self.assertEqual(status, 200)
        self.assertEqual([t["key"] for t in body["templates"]],
                         ["address_change", "document_request"])
        status, body, _ = self.get("/v1/templates/address_change")
        self.assertEqual(status, 200)
        self.assertEqual(body["template"]["fields"][0]["name"], "street_address")

    def test_a_missing_template_is_a_404_with_a_code(self):
        status, body, _ = self.get("/v1/templates/nothing_here")
        self.assertEqual((status, body["code"]), (404, "unknown_template"))

    def test_a_bad_template_is_refused_with_the_reason(self):
        status, body, _ = self.post("/v1/templates", {"template": {"key": "Bad Key"}})
        self.assertEqual((status, body["code"]), (400, "bad_template"))
        self.assertIn("key", body["error"])

    def test_linking_a_model_that_is_not_there_is_refused_up_front(self):
        body = copy.deepcopy(ADDRESS)
        body["model_id"] = "mdl-20200101-000000000-0000"
        status, reply, _ = self.post("/v1/templates", {"template": body})
        self.assertEqual((status, reply["code"]), (404, "not_found"))

    def test_deleting_by_key(self):
        status, body, _ = self.post("/v1/templates/document_request/delete")
        self.assertEqual((status, body["deleted"]), (200, "document_request"))
        status, _, _ = self.get("/v1/templates/document_request")
        self.assertEqual(status, 404)

    def test_one_library_s_templates_are_not_another_s(self):
        status, body, _ = self.get("/v1/templates", token=self.stranger)
        self.assertEqual((status, body["templates"]), (200, []))

    def test_everything_needs_a_token(self):
        for method, path in (("GET", "/v1/templates"), ("POST", "/v1/bot/turn"),
                             ("GET", "/v1/templates/address_change")):
            with self.subTest(path):
                status, body, _ = self.call(method, path,
                                            {} if method == "POST" else None, "")
                self.assertEqual((status, body["code"]), (401, "no_token"))

    def test_a_pinned_token_sees_only_templates_for_its_model_and_changes_none(self):
        status, body, _ = self.get("/v1/templates", token=self.pinned)
        self.assertEqual(body["templates"], [])
        linked = copy.deepcopy(ADDRESS)
        linked["key"] = "policy_intake"
        linked["model_id"] = self.model_id
        self.assertEqual(self.post("/v1/templates", {"template": linked})[0], 200)
        status, body, _ = self.get("/v1/templates", token=self.pinned)
        self.assertEqual([t["key"] for t in body["templates"]], ["policy_intake"])
        status, body, _ = self.post("/v1/templates", {"template": ADDRESS},
                                    token=self.pinned)
        self.assertEqual((status, body["code"]), (403, "out_of_scope"))
        status, body, _ = self.get("/v1/templates/address_change", token=self.pinned)
        self.assertEqual((status, body["code"]), (403, "out_of_scope"))


class TestTurnEndpoint(BotRestCase):
    def test_the_ask_over_http(self):
        status, reply, _ = self.turn({
            "input": {"type": "text", "text": THE_ASK, "via": "speech"},
            "context": {"current": {"city": "San Francisco", "street_address": "55 Market St"}},
        })
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["intent"]["template"], "address_change")
        self.assertEqual(reply["form"]["changes"]["city"],
                         {"before": "San Francisco", "after": "Irvine"})
        self.assertIn("fill_form", [a["id"] for a in reply["actions"]])

        status, reply, _ = self.turn({"input": {"type": "action", "action": "fill_form"},
                                      "state": reply["state"]})
        self.assertEqual(reply["effect"]["type"], "fill_form")

    def test_refusals_carry_the_contract_s_codes(self):
        cases = [
            ({"input": {"type": "audio"}}, 400, "bad_input"),
            ({"input": {"type": "text", "text": "hi"}, "state": {"v": 9}}, 400, "bad_state"),
            ({"input": {"type": "action", "action": "submit"}}, 409, "stale_action"),
            ({"input": {"type": "text", "text": "hi"},
              "context": {"templates": ["nope"]}}, 404, "unknown_template"),
        ]
        for body, status, code in cases:
            with self.subTest(code):
                got, reply, _ = self.turn(body)
                self.assertEqual((got, reply["code"]), (status, code))

    def test_context_templates_narrows_the_conversation(self):
        _, reply, _ = self.turn({"input": {"type": "text", "text": THE_ASK},
                                 "context": {"templates": ["document_request"]}})
        self.assertNotEqual(reply["intent"]["template"], "address_change")

    def test_a_template_linked_to_a_model_is_completed_by_it(self):
        linked = copy.deepcopy(ADDRESS)
        linked.update(key="policy_intake", model_id=self.model_id, fields=[
            {"name": "country", "label": "Country", "semantic_type": "country",
             "required": True, "options": ["US", "CA"]},
            {"name": "currency", "label": "Currency", "semantic_type": "enum",
             "required": True, "options": ["USD", "CAD"]},
        ], examples=["set up a policy"])
        self.assertEqual(self.post("/v1/templates", {"template": linked})[0], 200)
        status, reply, _ = self.turn({"input": {"type": "text",
                                                "text": "set up a policy, country CA"}},
                                     token=self.pinned)
        self.assertEqual(status, 200, reply)
        row = next(r for r in reply["form"]["fields"] if r["name"] == "currency")
        self.assertEqual((row["after"], row["source"]), ("CAD", "model"))

    def test_the_client_ships_the_chat_pieces(self):
        with urllib.request.urlopen(self.base + "/client/fillerai.js") as response:
            text = response.read()
        for name in (b"class BotChat", b"class ChatWidget", b"class SpeechInput"):
            self.assertIn(name, text)
        for page in ("/client/chat.html", "/client/fillerai-chat.css"):
            with urllib.request.urlopen(self.base + page) as response:
                self.assertEqual(response.status, 200)


class TestTheUiTryItChat(unittest.TestCase):
    """``/api/bot`` is the same turn behind the UI's own credential."""

    def setUp(self):
        self._was = (server_module.DATABASE, server_module.AUTH, server_module.LIBRARY)
        server_module.DATABASE, server_module.AUTH = None, None
        self.dir = tempfile.mkdtemp(prefix="fillerai-bot-ui-")
        server_module.LIBRARY = Store(self.dir)
        rest.forget()

    def tearDown(self):
        (server_module.DATABASE, server_module.AUTH, server_module.LIBRARY) = self._was
        rest.forget()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_starter_edit_and_try(self):
        listed = server_module.api_bot_templates({})
        self.assertEqual(listed["templates"], [])
        self.assertEqual({s["key"] for s in listed["starters"]},
                         {"address_change", "document_request"})
        server_module.api_bot_starter({"key": "address_change"})
        template = server_module.api_bot_template({"key": "address_change"})["template"]
        template["examples"].append("we moved")
        saved = server_module.api_bot_save({"template": template})
        self.assertIn("we moved", saved["template"]["examples"])
        self.assertEqual(len(server_module.api_bot_templates({})["templates"]), 1)

        reply = server_module.api_bot_turn({
            "input": {"type": "text", "text": THE_ASK},
            "context": {"current": {"city": "San Francisco"}}})
        self.assertEqual(reply["form"]["changes"]["city"]["after"], "Irvine")

    def test_a_template_from_a_schema_in_the_library(self):
        model, _ = a_model()
        entry = server_module.LIBRARY.save_schema(model.schema)
        draft = server_module.api_bot_from_schema({"id": entry.id})
        self.assertEqual(draft["schema_id"], entry.id)
        self.assertIn("country", [f["name"] for f in draft["template"]["fields"]])
        saved = server_module.api_bot_save({"template": draft["template"],
                                            "schema_id": entry.id})
        self.assertEqual(server_module.LIBRARY.get(saved["entry_id"]).parent, entry.id)

    def test_refusals_come_back_as_ui_errors(self):
        with self.assertRaises(server_module.ApiError):
            server_module.api_bot_turn({"input": {"type": "text", "text": "hi"}})
        with self.assertRaises(server_module.ApiError):
            server_module.api_bot_save({"template": {"key": "x"}})

    def test_the_language_model_reader_is_off_unless_turned_on(self):
        self.assertFalse(server_module.BOT_LLM)
        self.assertIsNone(botrest.READER("anyone"))
        try:
            server_module.use_bot_llm(True)
            self.assertIsNotNone(botrest.READER("anyone"))
        finally:
            server_module.use_bot_llm(False)
        self.assertIsNone(botrest.READER("anyone"))


if __name__ == "__main__":
    unittest.main()
