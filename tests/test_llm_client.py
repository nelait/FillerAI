"""Building a request and reading a reply, without sending anything.

The whole point of putting the network behind one method is that everything
around it is ordinary testable code. So these run entirely offline, and the
one test that exercises the real transport does so against a stubbed opener
rather than a socket.

Two of these cover failures that arrive looking like successes, which are the
ones worth having a test for: a refusal comes back as HTTP 200 with no usable
content, and an answer cut off by the token limit comes back as valid-looking
JSON that is missing its closing brace.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.llm import cost
from fillerai.llm.client import Client, ReplyError
from fillerai.llm.config import ConfigError, Settings
from fillerai.llm.transport import (
    RecordedTransport,
    TransportError,
    UrllibTransport,
    digest,
)


def answer(text: str, *, stop: str = "end_turn", **rest) -> dict:
    return {
        "id": "msg_test",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        **rest,
    }


class TestSettings(unittest.TestCase):
    def test_each_task_has_its_own_default_model(self):
        rules = Settings.resolve("rules", environ={})
        typing = Settings.resolve("typing", environ={})
        self.assertNotEqual(rules.model, typing.model)

    def test_the_fallback_key_variable_is_read(self):
        settings = Settings.resolve("rules", environ={"ANTHROPIC_API_KEY": "abc123"})
        self.assertTrue(settings.configured)

    def test_our_own_variable_wins(self):
        settings = Settings.resolve("rules", environ={
            "ANTHROPIC_API_KEY": "fallback", "FILLERAI_LLM_KEY": "ours"})
        self.assertEqual(settings.key, "ours")

    def test_the_key_is_never_shown_in_full(self):
        settings = Settings.resolve("rules", environ={"FILLERAI_LLM_KEY": "sk-secret-1234"})
        shown = settings.redacted()
        self.assertNotIn("secret", shown)
        self.assertIn("1234", shown)

    def test_an_unset_key_says_so_rather_than_raising(self):
        self.assertEqual(Settings.resolve("rules", environ={}).redacted(), "not set")

    def test_asking_for_the_key_without_one_explains_itself(self):
        with self.assertRaises(ConfigError) as caught:
            Settings.resolve("rules", environ={}).require_key()
        self.assertIn("FILLERAI_LLM_KEY", str(caught.exception))

    def test_an_unknown_task_is_refused(self):
        with self.assertRaises(ValueError):
            Settings.resolve("astrology", environ={})

    def test_the_base_url_can_point_somewhere_else(self):
        settings = Settings.resolve("rules", environ={
            "FILLERAI_LLM_BASE_URL": "https://example.invalid/anthropic/"})
        self.assertEqual(settings.endpoint,
                         "https://example.invalid/anthropic/v1/messages")


class TestRequests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.resolve("rules", environ={"FILLERAI_LLM_KEY": "k"})

    def test_the_system_prompt_is_marked_cacheable(self):
        request = Client(self.settings).build(system="S", user="U")
        self.assertEqual(request["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_a_schema_becomes_output_config_not_the_old_parameter(self):
        request = Client(self.settings).build(
            system="S", user="U", output_schema={"type": "object"})
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("output_format", request)

    def test_thinking_is_not_pinned(self):
        """The default models run it adaptively; naming it would pin behaviour."""
        self.assertNotIn("thinking", Client(self.settings).build(system="S", user="U"))

    def test_the_digest_ignores_key_order(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))


class TestReading(unittest.TestCase):
    def test_the_answer_is_the_last_text_block_not_the_first(self):
        reply = Client.read({
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "the answer"},
            ],
            "stop_reason": "end_turn",
        })
        self.assertEqual(reply.text, "the answer")

    def test_a_refusal_is_raised_rather_than_parsed(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read({
                "content": [], "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "explanation": "no"},
            })
        self.assertIn("declined", str(caught.exception))

    def test_a_truncated_answer_is_caught_before_it_is_parsed(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read(answer('{"rules": [', stop="max_tokens"), structured=True)
        self.assertIn("cut off", str(caught.exception))

    def test_invalid_json_says_so(self):
        with self.assertRaises(ReplyError) as caught:
            Client.read(answer("not json at all"), structured=True)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_response_with_no_text_at_all_is_an_error(self):
        with self.assertRaises(ReplyError):
            Client.read({"content": [], "stop_reason": "end_turn"})

    def test_usage_comes_through(self):
        reply = Client.read(answer("hi"))
        self.assertEqual(reply.input_tokens, 10)
        self.assertEqual(reply.output_tokens, 5)


class TestRecordedTransport(unittest.TestCase):
    def test_an_unrecorded_request_raises_rather_than_reaching_out(self):
        transport = RecordedTransport([])
        with self.assertRaises(TransportError) as caught:
            transport.send({"model": "x"})
        self.assertIn("no recording", str(caught.exception))

    def test_a_digest_keyed_recording_is_matched_exactly(self):
        request = {"model": "x", "max_tokens": 1}
        transport = RecordedTransport([
            {"digest": digest(request), "response": answer("keyed")},
            {"digest": None, "response": answer("ordered")},
        ])
        self.assertEqual(transport.send(request)["content"][0]["text"], "keyed")

    def test_unkeyed_recordings_are_handed_out_in_order(self):
        transport = RecordedTransport([
            {"digest": None, "response": answer("first")},
            {"digest": None, "response": answer("second")},
        ])
        self.assertEqual(transport.send({})["content"][0]["text"], "first")
        self.assertEqual(transport.send({})["content"][0]["text"], "second")

    def test_it_remembers_what_it_was_sent(self):
        transport = RecordedTransport([{"digest": None, "response": answer("x")}])
        transport.send({"model": "m"})
        self.assertEqual(transport.sent, [{"model": "m"}])


class TestUrllibTransport(unittest.TestCase):
    """The real transport, with the socket stubbed out."""

    def setUp(self):
        self.settings = Settings.resolve("rules", environ={"FILLERAI_LLM_KEY": "k"})

    def test_the_request_carries_the_headers_the_api_needs(self):
        transport = UrllibTransport(self.settings)
        request = transport._request(b"{}")
        self.assertEqual(request.get_header("X-api-key"), "k")
        self.assertIsNotNone(request.get_header("Anthropic-version"))
        self.assertEqual(request.full_url, self.settings.endpoint)

    def test_a_400_is_not_retried(self):
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", {},
                _Body(json.dumps({"error": {"message": "bad model"}})))

        transport = UrllibTransport(self.settings, retries=3, sleep=lambda _: None)
        with _patched(opener):
            with self.assertRaises(TransportError) as caught:
                transport.send({})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("bad model", str(caught.exception))

    def test_a_successful_call_is_parsed(self):
        def opener(request, timeout=None):
            self.assertEqual(json.loads(request.data)["model"], "m")
            return _Body(json.dumps(answer("hello")))

        transport = UrllibTransport(self.settings, sleep=lambda _: None)
        with _patched(opener):
            response = transport.send({"model": "m"})
        self.assertEqual(response["content"][0]["text"], "hello")

    def test_a_429_is_retried_then_given_up_on(self):
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, _Body("{}"))

        transport = UrllibTransport(self.settings, retries=2, sleep=lambda _: None)
        with _patched(opener):
            with self.assertRaises(TransportError):
                transport.send({})
        self.assertEqual(len(attempts), 3)


class _Body:
    """Just enough of a file object for HTTPError to carry a message.

    ``close`` is not optional: HTTPError hands the object to a tempfile
    closer, which calls it during garbage collection and prints a traceback
    to stderr if it is missing, long after the test has passed.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _patched:
    def __init__(self, opener):
        self.opener = opener

    def __enter__(self):
        import urllib.request

        self._real = urllib.request.urlopen
        urllib.request.urlopen = _AsContext(self.opener)

    def __exit__(self, *exc):
        import urllib.request

        urllib.request.urlopen = self._real


class _AsContext:
    def __init__(self, opener):
        self.opener = opener

    def __call__(self, request, timeout=None):
        return self.opener(request, timeout=timeout)


class TestCost(unittest.TestCase):
    def test_a_known_model_is_priced(self):
        estimate = cost.estimate("claude-opus-5", "x" * 4000, 1000)
        self.assertTrue(estimate.priced)
        self.assertGreater(estimate.dollars, 0)

    def test_an_unknown_model_says_it_does_not_know(self):
        estimate = cost.estimate("some-local-model", "x" * 400, 100)
        self.assertFalse(estimate.priced)
        self.assertIn("cost unknown", " ".join(estimate.describe()))

    def test_a_ceiling_refuses_an_expensive_run(self):
        estimate = cost.estimate("claude-opus-5", "x" * 4_000_000, 100_000)
        with self.assertRaises(cost.SpendRefused):
            cost.enforce(estimate, 1.0)

    def test_no_ceiling_refuses_nothing(self):
        cost.enforce(cost.estimate("claude-opus-5", "x" * 4_000_000, 100_000), None)

    def test_an_unpriced_model_cannot_trip_the_ceiling(self):
        cost.enforce(cost.estimate("mystery", "x" * 4_000_000, 100_000), 0.01)


if __name__ == "__main__":
    unittest.main()
