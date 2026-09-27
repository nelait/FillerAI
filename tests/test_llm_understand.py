"""The language-model reading of a chat phrase, without a network.

What matters is not that a model reads well - nothing here can say that
without a key - but that whatever it says is believed only as far as it
checks out, and that any failure is a turn read locally rather than a turn
lost.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.bot import take_turn
from fillerai.bot.template import starters
from fillerai.llm import understand
from fillerai.llm.config import Settings
from fillerai.llm.providers import Reply

TEMPLATES = list(starters().values())
ADDRESS = starters()["address_change"]
SETTINGS = Settings.resolve("chat", environ={"FILLERAI_LLM_KEY": "sk-ant-test"})


class Canned:
    """A client that answers with one payload, or raises."""

    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.asked = payload, error, []

    def ask(self, **request):
        self.asked.append(request)
        if self.error is not None:
            raise self.error
        return Reply(text=json.dumps(self.payload), model="m", stop_reason="end_turn",
                     data=self.payload)


def read(payload=None, error=None, text="we're moving in with my sister in Tustin",
         active=None, settings=SETTINGS):
    client = Canned(payload, error)
    reading = understand.reader(settings, client=client)(TEMPLATES, active, text)
    return reading, client


class TestTheLanguageModelReader(unittest.TestCase):
    def test_a_good_answer_is_taken(self):
        reading, client = read({"template": "address_change", "confidence": 0.9,
                                "values": [{"field": "city", "value": "Tustin",
                                            "said_before": ""}]})
        self.assertEqual((reading.how, reading.template), ("llm", "address_change"))
        self.assertEqual(reading.values["city"].value, "Tustin")
        sent = json.loads(client.asked[0]["user"])
        self.assertEqual(sent["message"], "we're moving in with my sister in Tustin")
        self.assertEqual([t["key"] for t in sent["templates"]],
                         ["address_change", "document_request"])
        self.assertEqual(client.asked[0]["output_schema"], understand.OUTPUT_SCHEMA)

    def test_what_it_cannot_back_up_is_dropped(self):
        reading, _ = read({"template": "address_change", "confidence": 0.9, "values": [
            {"field": "shoe_size", "value": "10", "said_before": ""},
            {"field": "country", "value": "Atlantis", "said_before": ""},
            {"field": "state", "value": "california", "said_before": ""},
        ]})
        self.assertEqual(set(reading.values), {"state"})
        self.assertEqual(reading.values["state"].value, "CA")
        self.assertEqual([p.field for p in reading.problems], ["country"])

    def test_a_template_that_is_not_on_offer_is_no_template(self):
        reading, _ = read({"template": "delete_account", "confidence": 1.0,
                           "values": []}, text="close my account")
        self.assertIsNone(reading.template)

    def test_any_failure_is_a_turn_read_locally(self):
        for error in (RuntimeError("down"), ValueError("not json")):
            with self.subTest(type(error).__name__):
                reading, _ = read(error=error, text="change my city to Irvine")
                self.assertEqual(reading.how, "local")
                self.assertEqual(reading.values["city"].value, "Irvine")

    def test_no_key_is_a_turn_read_locally_without_asking(self):
        keyless = Settings.resolve("chat", environ={})
        reading, client = read({"template": "address_change", "values": []},
                               text="change my city to Irvine", settings=keyless)
        self.assertEqual((reading.how, client.asked), ("local", []))

    def test_it_drives_a_whole_turn(self):
        client = Canned({"template": "address_change", "confidence": 0.8, "values": [
            {"field": "city", "value": "Tustin", "said_before": "San Francisco"}]})
        reply = take_turn(TEMPLATES, {
            "input": {"type": "text", "text": "we're moving in with my sister in Tustin"},
            "context": {"current": {"city": "San Francisco"}}},
            reader=understand.reader(SETTINGS, client=client))
        self.assertEqual(reply["intent"]["how"], "llm")
        self.assertEqual(reply["form"]["changes"]["city"]["after"], "Tustin")

    def test_the_answer_shape_is_strict_ready(self):
        from fillerai.llm.providers import strict_ready
        self.assertTrue(strict_ready(understand.OUTPUT_SCHEMA))

    def test_the_chat_task_has_a_default_model_on_both_services(self):
        for provider in ("anthropic", "openai"):
            with self.subTest(provider):
                self.assertTrue(Settings.resolve("chat", provider=provider, environ={}).model)


if __name__ == "__main__":
    unittest.main()
