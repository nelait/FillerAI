"""Two services, one question, and everything that differs between them.

These are the tests that stop "FillerAI supports OpenAI" from meaning "the
key is read and the call fails". Each provider gets the same three questions
asked of it - what does the request look like, how is a good reply read, how is
a bad one caught - and then one test runs the *same* proposed rules through
both readers and asserts the validation gate cannot tell which service wrote
them. That last one is the point of the whole layer.

Everything here is offline. The fixtures are hand-authored and say so.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fillerai
from fillerai.llm import cost, providers
from fillerai.llm.client import Client
from fillerai.llm.config import (
    BASE_URL_VARIABLE,
    KEY_VARIABLE,
    MODEL_VARIABLE,
    PROVIDER_VARIABLE,
    ConfigError,
    Settings,
)
from fillerai.llm.providers import Anthropic, OpenAI, ReplyError, strict_ready
from fillerai.llm.transport import (
    OpenAiSdkTransport,
    RecordedTransport,
    SdkTransport,
    UrllibTransport,
    sdk_for,
)

FIXTURES = ROOT / "tests" / "fixtures" / "llm"
FORM = ROOT / "examples" / "member_enrollment.fields.json"


def openai_reply(text: str, *, finish: str = "stop", refusal=None, **rest) -> dict:
    return {
        "id": "chatcmpl_test",
        "model": "gpt-5",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text, "refusal": refusal},
            "finish_reason": finish,
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        **rest,
    }


class TestChoosingTheProvider(unittest.TestCase):
    """Which service a bare command talks to, and what decided it."""

    def test_nothing_set_means_anthropic(self):
        settings = Settings.resolve("rules", environ={})
        self.assertEqual(settings.provider, "anthropic")
        self.assertEqual(settings.provider_reason, "the default")

    def test_an_openai_key_is_enough_on_its_own(self):
        """The whole point: exporting OPENAI_API_KEY should just work."""
        settings = Settings.resolve("rules", environ={"OPENAI_API_KEY": "sk-proj-x"})
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.key, "sk-proj-x")
        self.assertTrue(settings.configured)

    def test_the_environment_variable_wins_over_the_keys(self):
        settings = Settings.resolve("rules", environ={
            PROVIDER_VARIABLE: "openai", "ANTHROPIC_API_KEY": "sk-ant-x"})
        self.assertEqual(settings.provider, "openai")

    def test_an_argument_wins_over_the_environment(self):
        settings = Settings.resolve("rules", provider="anthropic", environ={
            PROVIDER_VARIABLE: "openai"})
        self.assertEqual(settings.provider, "anthropic")

    def test_a_model_name_gives_its_provider_away(self):
        for model, expected in (("gpt-4.1", "openai"), ("o3", "openai"),
                                ("claude-opus-5", "anthropic")):
            with self.subTest(model=model):
                settings = Settings.resolve("rules", model=model, environ={})
                self.assertEqual(settings.provider, expected)

    def test_the_model_variable_counts_as_naming_one(self):
        settings = Settings.resolve("rules", environ={MODEL_VARIABLE: "gpt-5-mini"})
        self.assertEqual(settings.provider, "openai")

    def test_the_neutral_key_is_read_for_its_prefix(self):
        anthropic = Settings.resolve("rules", environ={KEY_VARIABLE: "sk-ant-api03-x"})
        openai = Settings.resolve("rules", environ={KEY_VARIABLE: "sk-proj-x"})
        self.assertEqual(anthropic.provider, "anthropic")
        self.assertEqual(openai.provider, "openai")

    def test_two_keys_and_no_preference_says_so_rather_than_guessing(self):
        settings = Settings.resolve("rules", environ={
            "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b"})
        self.assertEqual(settings.provider, providers.DEFAULT)
        self.assertIn("$OPENAI_API_KEY", settings.provider_reason)

    def test_the_neutral_key_beats_the_provider_s_own(self):
        settings = Settings.resolve("rules", provider="openai", environ={
            KEY_VARIABLE: "ours", "OPENAI_API_KEY": "theirs"})
        self.assertEqual(settings.key, "ours")

    def test_an_unknown_provider_names_the_ones_that_exist(self):
        with self.assertRaises(ValueError) as caught:
            Settings.resolve("rules", provider="skynet", environ={})
        self.assertIn("openai", str(caught.exception))

    def test_each_provider_has_its_own_per_task_models(self):
        for name in providers.NAMES:
            with self.subTest(provider=name):
                rules = Settings.resolve("rules", provider=name, environ={})
                typing = Settings.resolve("typing", provider=name, environ={})
                self.assertNotEqual(rules.model, typing.model)
                self.assertTrue(providers.get(name).recognises(rules.model))

    def test_the_endpoint_follows_the_provider(self):
        anthropic = Settings.resolve("rules", provider="anthropic", environ={})
        openai = Settings.resolve("rules", provider="openai", environ={})
        self.assertTrue(anthropic.endpoint.endswith("/v1/messages"))
        self.assertTrue(openai.endpoint.endswith("/v1/chat/completions"))
        self.assertIn("api.openai.com", openai.endpoint)

    def test_a_gateway_keeps_the_provider_s_path(self):
        settings = Settings.resolve("rules", provider="openai", environ={
            BASE_URL_VARIABLE: "https://gateway.invalid/openai/"})
        self.assertEqual(settings.endpoint,
                         "https://gateway.invalid/openai/v1/chat/completions")
        self.assertTrue(settings.custom_base_url)

    def test_the_missing_key_message_names_the_right_variable(self):
        with self.assertRaises(ConfigError) as caught:
            Settings.resolve("rules", provider="openai", environ={}).require_key()
        message = str(caught.exception)
        self.assertIn("OPENAI_API_KEY", message)
        self.assertNotIn("ANTHROPIC_API_KEY", message)

    def test_a_key_is_still_never_shown_in_full(self):
        settings = Settings.resolve("rules", environ={"OPENAI_API_KEY": "sk-proj-secret-9876"})
        self.assertNotIn("secret", settings.redacted())
        self.assertIn("9876", settings.redacted())


class TestOpenAiRequests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.resolve(
            "rules", provider="openai", environ={KEY_VARIABLE: "k"})
        self.request = Client(self.settings).build(
            system="S", user="U", output_schema={"type": "object"}, max_tokens=999)

    def test_the_prompt_becomes_two_messages_not_a_system_field(self):
        roles = [m["role"] for m in self.request["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertNotIn("system", self.request)

    def test_it_asks_for_completion_tokens_by_the_name_that_still_works(self):
        """Reasoning models refuse `max_tokens` outright."""
        self.assertEqual(self.request["max_completion_tokens"], 999)
        self.assertNotIn("max_tokens", self.request)

    def test_a_schema_becomes_a_named_response_format(self):
        fmt = self.request["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["name"])
        self.assertNotIn("output_config", self.request)

    def test_temperature_is_left_alone(self):
        self.assertNotIn("temperature", self.request)

    def test_no_schema_means_no_response_format(self):
        plain = Client(self.settings).build(system="S", user="U")
        self.assertNotIn("response_format", plain)

    def test_the_key_goes_in_a_bearer_header(self):
        request = UrllibTransport(self.settings)._request(b"{}")
        self.assertEqual(request.get_header("Authorization"), "Bearer k")
        self.assertIsNone(request.get_header("X-api-key"))
        self.assertIsNone(request.get_header("Anthropic-version"))


class TestStrictMode(unittest.TestCase):
    """Whether OpenAI can be asked to *guarantee* the shape, per schema."""

    def test_the_rules_schema_cannot_be_strict(self):
        """`when` is a map with keys nobody knows in advance."""
        from fillerai.llm import prompts

        self.assertFalse(strict_ready(prompts.RULES_OUTPUT_SCHEMA))

    def test_the_schema_is_still_sent_when_it_cannot_be(self):
        from fillerai.llm import prompts

        settings = Settings.resolve("rules", provider="openai", environ={KEY_VARIABLE: "k"})
        request = Client(settings).build(
            system="S", user="U", output_schema=prompts.RULES_OUTPUT_SCHEMA)
        schema = request["response_format"]["json_schema"]
        self.assertIs(schema["strict"], False)
        self.assertIn("rules", schema["schema"]["properties"])

    def test_a_closed_schema_can_be(self):
        self.assertTrue(strict_ready({
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }))

    def test_a_property_left_out_of_required_cannot(self):
        self.assertFalse(strict_ready({
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }))

    def test_an_open_object_nested_in_an_array_is_caught(self):
        self.assertFalse(strict_ready({
            "type": "array",
            "items": {"type": "object", "properties": {},
                      "required": [], "additionalProperties": True},
        }))


class TestOpenAiReading(unittest.TestCase):
    def test_a_good_answer_is_read(self):
        reply = Client.read(openai_reply('{"rules": []}'), structured=True)
        self.assertEqual(reply.data, {"rules": []})
        self.assertEqual(reply.model, "gpt-5")

    def test_usage_is_normalised_onto_the_same_names(self):
        reply = Client.read(openai_reply("hi"))
        self.assertEqual(reply.input_tokens, 11)
        self.assertEqual(reply.output_tokens, 7)

    def test_a_refusal_is_raised_rather_than_parsed(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read(openai_reply(None, refusal="I can't help with that"))
        self.assertIn("declined", str(caught.exception))

    def test_a_truncated_answer_is_caught_before_it_is_parsed(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read(openai_reply('{"rules": [', finish="length"), structured=True)
        self.assertIn("cut off", str(caught.exception))

    def test_an_empty_answer_is_an_error(self):
        with self.assertRaises(ReplyError):
            Client.read(openai_reply(""))

    def test_no_choices_at_all_is_an_error(self):
        with self.assertRaises(ReplyError):
            Client.read({"choices": [], "model": "gpt-5"})

    def test_invalid_json_says_so(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read(openai_reply("sorry, here are the rules:"), structured=True)
        self.assertIn("not valid JSON", str(caught.exception))


class TestReadingWithoutBeingTold(unittest.TestCase):
    """A recording is a response and nothing else; the shape decides."""

    def test_an_openai_response_is_recognised(self):
        self.assertIs(providers.for_response(openai_reply("x")), OpenAI)

    def test_an_anthropic_response_is_recognised(self):
        self.assertIs(providers.for_response({
            "content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"}),
            Anthropic)

    def test_naming_the_provider_overrides_the_shape(self):
        with self.assertRaises(ReplyError):
            Client.read(openai_reply("x"), provider="anthropic")


class TestTheGateCannotTellWhoAnswered(unittest.TestCase):
    """The same proposed rules, read through both readers, check out the same.

    If this ever fails, the provider layer has leaked into the part of the
    project that is supposed to be about forms.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = fillerai.extract_spec(FORM)

    def _proposals(self, fixture: str, provider: str):
        from fillerai.llm import rules as llm_rules

        settings = Settings.resolve("rules", provider=provider,
                                    environ={KEY_VARIABLE: "k"})
        client = Client(settings, RecordedTransport.from_path(FIXTURES / fixture))
        return llm_rules.propose(self.schema, client=client, settings=settings)

    def test_both_fixtures_reach_the_same_verdicts(self):
        through_anthropic = self._proposals("rules_member_enrollment.json", "anthropic")
        through_openai = self._proposals("rules_member_enrollment.openai.json", "openai")
        self.assertEqual(
            [(v.proposal.field, v.problem) for v in through_anthropic.verdicts],
            [(v.proposal.field, v.problem) for v in through_openai.verdicts],
        )
        self.assertTrue(through_openai.kept)

    def test_the_openai_run_sent_an_openai_shaped_request(self):
        from fillerai.llm import rules as llm_rules

        settings = Settings.resolve("rules", provider="openai", environ={KEY_VARIABLE: "k"})
        transport = RecordedTransport.from_path(
            FIXTURES / "rules_member_enrollment.openai.json")
        llm_rules.propose(self.schema, client=Client(settings, transport),
                          settings=settings)
        self.assertIn("max_completion_tokens", transport.sent[0])


class TestTheRightSdkOrNone(unittest.TestCase):
    """An installed `anthropic` is no reason to send an OpenAI request to it."""

    def test_each_sdk_answers_only_for_its_own_provider(self):
        self.assertEqual(SdkTransport.provider, "anthropic")
        self.assertEqual(OpenAiSdkTransport.provider, "openai")

    def test_an_absent_sdk_falls_back_to_the_standard_library(self):
        if SdkTransport.available() or OpenAiSdkTransport.available():
            self.skipTest("an SDK is installed in this environment")
        for name in providers.NAMES:
            with self.subTest(provider=name):
                settings = Settings.resolve("rules", provider=name, environ={})
                self.assertIsNone(sdk_for(settings))

    def test_the_sdk_base_url_is_only_overridden_when_we_were_pointed_elsewhere(self):
        from fillerai.llm.transport import _sdk_base_url

        default = Settings.resolve("rules", provider="openai", environ={})
        gateway = Settings.resolve("rules", provider="openai", environ={
            BASE_URL_VARIABLE: "https://gateway.invalid"})
        self.assertIsNone(_sdk_base_url(default))
        self.assertEqual(_sdk_base_url(gateway), "https://gateway.invalid/v1")


class TestOpenAiCost(unittest.TestCase):
    def test_the_default_models_are_priced(self):
        for name in providers.NAMES:
            for task in ("rules", "typing"):
                model = providers.get(name).task_models[task]
                with self.subTest(model=model):
                    self.assertTrue(cost.estimate(model, "x" * 400, 100).priced,
                                    f"{model} has no price in cost.PRICES")

    def test_the_ceiling_still_bites_on_an_openai_model(self):
        estimate = cost.estimate("gpt-5", "x" * 4_000_000, 100_000)
        with self.assertRaises(cost.SpendRefused):
            cost.enforce(estimate, 1.0)


class TestTheFixturesSayWhatTheyAre(unittest.TestCase):
    def test_both_still_admit_they_were_written_by_hand(self):
        for name in ("rules_member_enrollment.json",
                     "rules_member_enrollment.openai.json"):
            with self.subTest(fixture=name):
                note = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["_note"]
                self.assertIn("HAND-AUTHORED", note)


if __name__ == "__main__":
    unittest.main()
