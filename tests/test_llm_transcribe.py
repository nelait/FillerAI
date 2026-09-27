"""Transcribing a recording with an OpenAI key, for the chat's microphone.

Nothing here reaches OpenAI. The upload is checked against a local server
that reads the multipart body the way the real endpoint would, and the rest
against a stand-in for the post. What matters is which key is used (never an
Anthropic one), what is sent, that it is off unless turned on, and that every
failure comes back as a sentence rather than a stack trace.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fillerai.llm import transcribe, transport
from fillerai.web import botrest
from fillerai.web import server as server_module

from test_rest import RestCase

AUDIO = b"OggS\x00fake-opus-frames"


class Recorder:
    """Stands in for the upload and remembers what it was given."""

    def __init__(self, answer=None, error=None):
        self.answer = {"text": "please update my city to Irvine"} if answer is None else answer
        self.error = error
        self.calls = []

    def __call__(self, url, headers, fields, files, **_):
        self.calls.append({"url": url, "headers": headers, "fields": fields, "files": files})
        if self.error:
            raise self.error
        return self.answer


class TestWhichKey(unittest.TestCase):
    def test_openai_s_own_variable_first(self):
        self.assertEqual(transcribe.openai_key({"OPENAI_API_KEY": "sk-one",
                                                "FILLERAI_LLM_KEY": "sk-two"}), "sk-one")

    def test_the_shared_variable_only_when_it_is_an_openai_key(self):
        self.assertEqual(transcribe.openai_key({"FILLERAI_LLM_KEY": "sk-proj-x"}), "sk-proj-x")
        self.assertIsNone(transcribe.openai_key({"FILLERAI_LLM_KEY": "sk-ant-x"}),
                          "an Anthropic key is never sent to OpenAI")
        self.assertIsNone(transcribe.openai_key({"ANTHROPIC_API_KEY": "sk-ant-x"}))


class TestTranscriber(unittest.TestCase):
    def test_what_is_sent(self):
        post = Recorder()
        text = transcribe.transcriber({"OPENAI_API_KEY": "sk-test"}, post=post)(
            AUDIO, "audio/webm;codecs=opus", "en-US")
        self.assertEqual(text, "please update my city to Irvine")
        call = post.calls[0]
        self.assertEqual(call["url"], "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(call["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(call["fields"], {"model": "gpt-4o-mini-transcribe",
                                          "response_format": "json", "language": "en"})
        self.assertEqual(call["files"]["file"], ("speech.webm", "audio/webm", AUDIO))

    def test_the_model_and_host_can_be_changed(self):
        post = Recorder()
        transcribe.transcriber({"OPENAI_API_KEY": "sk-test",
                                "FILLERAI_TRANSCRIBE_MODEL": "whisper-1",
                                "FILLERAI_TRANSCRIBE_BASE_URL": "http://proxy.local/",
                                "FILLERAI_LLM_BASE_URL": "http://other.local"}, post=post)(
            AUDIO, "audio/ogg", "")
        self.assertEqual(post.calls[0]["url"], "http://proxy.local/v1/audio/transcriptions",
                         "the shared base URL may point at the other service")
        self.assertEqual(post.calls[0]["fields"]["model"], "whisper-1")
        self.assertNotIn("language", post.calls[0]["fields"])

    def test_every_failure_is_a_sentence(self):
        cases = [
            ({}, AUDIO, "audio/webm", "no OpenAI key"),
            ({"OPENAI_API_KEY": "sk-t"}, b"", "audio/webm", "empty"),
            ({"OPENAI_API_KEY": "sk-t"}, AUDIO, "video/mp4", "can't transcribe video/mp4"),
            ({"OPENAI_API_KEY": "sk-t"}, b"x" * (transcribe.MAX_AUDIO_BYTES + 1),
             "audio/webm", "too long"),
        ]
        for env, audio, mime, said in cases:
            with self.subTest(said):
                with self.assertRaises(transcribe.TranscribeError) as caught:
                    transcribe.transcriber(env, post=Recorder())(audio, mime, "")
                self.assertIn(said, str(caught.exception))

    def test_a_refused_upload_says_what_the_service_said(self):
        post = Recorder(error=transport.TransportError(
            "the speech service answered 401: Incorrect API key", status=401))
        with self.assertRaises(transcribe.TranscribeError) as caught:
            transcribe.transcriber({"OPENAI_API_KEY": "sk-t"}, post=post)(AUDIO, "audio/webm", "")
        self.assertIn("Incorrect API key", str(caught.exception))


class TestTheUpload(unittest.TestCase):
    """``post_form`` against a server that reads the body as multipart."""

    def test_a_multipart_body_the_far_end_can_read(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                message = BytesParser(policy=HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    seen[name] = (part.get_filename(), part.get_content_type(),
                                  part.get_payload(decode=True))
                seen["auth"] = self.headers["Authorization"]
                out = json.dumps({"text": "hello"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            text = transcribe.transcriber({"OPENAI_API_KEY": "sk-t",
                                           "FILLERAI_TRANSCRIBE_BASE_URL":
                                           f"http://127.0.0.1:{httpd.server_address[1]}"})(
                AUDIO, "audio/webm", "en")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(text, "hello")
        self.assertEqual(seen["file"], ("speech.webm", "audio/webm", AUDIO))
        self.assertEqual(seen["model"][2], b"gpt-4o-mini-transcribe")
        self.assertEqual(seen["language"][2], b"en")
        self.assertEqual(seen["auth"], "Bearer sk-t")

    def test_an_unreachable_service_is_a_transport_error(self):
        with self.assertRaises(transport.TransportError):
            transport.post_form("http://127.0.0.1:9/v1/audio/transcriptions", {}, {}, {},
                                timeout=2)


class TestOverHttp(RestCase):
    def tearDown(self):
        botrest.TRANSCRIBER = lambda owner: None

    def send(self, body, token=None):
        return self.post("/v1/bot/transcribe", body, token=token)

    def test_off_unless_turned_on(self):
        status, body, _ = self.send({"audio": base64.b64encode(AUDIO).decode(),
                                     "mime": "audio/webm"})
        self.assertEqual((status, body["code"]), (409, "transcription_off"))

    def test_a_recording_comes_back_as_text(self):
        got = []
        botrest.TRANSCRIBER = lambda owner: (lambda audio, mime, lang: got.append(
            (audio, mime, lang)) or "change my city to Irvine")
        status, body, _ = self.send({"audio": base64.b64encode(AUDIO).decode(),
                                     "mime": "audio/webm", "language": "en-US"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"text": "change my city to Irvine", "via": "speech"})
        self.assertEqual(got, [(AUDIO, "audio/webm", "en-US")])

    def test_refusals_carry_codes(self):
        def failing(audio, mime, lang):
            raise transcribe.TranscribeError("the speech service answered 401: bad key")

        botrest.TRANSCRIBER = lambda owner: failing
        cases = [
            ({"mime": "audio/webm"}, 400, "bad_input"),
            ({"audio": "not base64!", "mime": "audio/webm"}, 400, "bad_input"),
            ({"audio": base64.b64encode(AUDIO).decode(), "mime": "text/plain"}, 400, "bad_input"),
            ({"audio": base64.b64encode(AUDIO).decode(), "mime": "audio/webm"}, 502,
             "transcription_failed"),
        ]
        for body, status, code in cases:
            with self.subTest(code=code, status=status):
                got, reply, _ = self.send(body)
                self.assertEqual((got, reply["code"]), (status, code))

    def test_it_needs_a_token(self):
        status, body, _ = self.call("POST", "/v1/bot/transcribe", {}, "")
        self.assertEqual((status, body["code"]), (401, "no_token"))


class TestTheUiSwitch(unittest.TestCase):
    def tearDown(self):
        server_module.use_bot_transcribe(False)

    def test_off_by_default_and_on_when_asked(self):
        self.assertFalse(server_module.BOT_TRANSCRIBE)
        with self.assertRaises(server_module.ApiError) as caught:
            server_module.api_bot_transcribe({"audio": "AA==", "mime": "audio/webm"})
        self.assertIn("--bot-transcribe", str(caught.exception))
        server_module.use_bot_transcribe(True)
        self.assertIsNotNone(botrest.TRANSCRIBER("anyone"))


if __name__ == "__main__":
    unittest.main()
