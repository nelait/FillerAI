"""Turning recorded speech into text, for the chat's microphone.

The chat's microphone normally uses the browser's own recogniser, which in
Chrome and Edge sends the audio to the browser vendor's speech service. Where
that service is unreachable - a VPN, a corporate proxy, a browser policy -
the microphone hears nothing, however well the microphone itself works. This
is the second route: the page records the audio itself and AIrForms sends it
to OpenAI's transcription endpoint with the configured OpenAI key.

**Off unless turned on**, like reading chat phrases with a model: ``serve
--bot-transcribe`` or ``FILLERAI_BOT_TRANSCRIBE=1``. A key being present is not
enough, because this sends a person's voice to a third party.

**OpenAI only.** Anthropic's API does not transcribe audio, so this looks for
an OpenAI key whichever service the rest of the language-model features use:
``$OPENAI_API_KEY``, one typed into Settings for OpenAI, or ``$FILLERAI_LLM_KEY``
when that is an OpenAI key.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from . import transport
from .config import KEY_VARIABLE
from .providers import OpenAI

MODEL_VARIABLE = "FILLERAI_TRANSCRIBE_MODEL"
BASE_URL_VARIABLE_OWN = "FILLERAI_TRANSCRIBE_BASE_URL"
#: Cheap, quick and good at short phrases; ``whisper-1`` works too.
DEFAULT_MODEL = "gpt-4o-mini-transcribe"
PATH = "/v1/audio/transcriptions"
#: About two minutes of the compressed audio a browser records. A chat
#: message is a sentence, so anything bigger is a mistake, not a message.
MAX_AUDIO_BYTES = 4 * 1024 * 1024
#: What a browser's MediaRecorder produces, and what the endpoint takes.
EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
              "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
              "audio/aac": "m4a", "audio/x-m4a": "m4a"}


class TranscribeError(RuntimeError):
    """Why a recording did not become text, in words for the person."""


def openai_key(environ: dict[str, str] | None = None) -> str | None:
    """The OpenAI key to use, or None. An Anthropic key is never sent to OpenAI."""
    env = os.environ if environ is None else environ
    own = env.get(OpenAI.key_variable)
    if own:
        return own
    shared = env.get(KEY_VARIABLE) or ""
    if shared.startswith(OpenAI.key_prefix) and not shared.startswith("sk-ant-"):
        return shared
    return None


def extension(mime: str) -> str:
    base = (mime or "").split(";", 1)[0].strip().lower()
    if base not in EXTENSIONS:
        raise TranscribeError(f"can't transcribe {base or 'audio of no stated type'}; "
                              "send webm, ogg, mp4, mp3, m4a or wav")
    return EXTENSIONS[base]


def transcriber(environ: dict[str, str] | None = None, *,
                post: Callable[..., dict[str, Any]] | None = None
                ) -> Callable[[bytes, str, str], str]:
    """A function ``(audio, mime, language) -> text`` bound to one key.

    ``post`` is injectable so tests never open a socket; it defaults to
    :func:`fillerai.llm.transport.post_form`.
    """
    env = dict(os.environ if environ is None else environ)
    key = openai_key(env)
    # Its own variable, not $FILLERAI_LLM_BASE_URL: that one may point at a
    # proxy for the other service, which must not receive the audio.
    base = env.get(BASE_URL_VARIABLE_OWN) or OpenAI.default_base_url
    model = env.get(MODEL_VARIABLE) or DEFAULT_MODEL
    send = post or transport.post_form

    def transcribe(audio: bytes, mime: str, language: str = "") -> str:
        if not key:
            raise TranscribeError(
                "speech is set to go through OpenAI, but there is no OpenAI key. Add one "
                f"in Settings, or export ${OpenAI.key_variable} before starting the server.")
        if not audio:
            raise TranscribeError("the recording was empty")
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscribeError("that recording is too long for one message")
        fields = {"model": model, "response_format": "json"}
        lang = (language or "").split("-", 1)[0].lower()
        if len(lang) == 2 and lang.isalpha():
            fields["language"] = lang
        try:
            reply = send(base.rstrip("/") + PATH, OpenAI.headers(key),
                         fields, {"file": (f"speech.{extension(mime)}", mime.split(";")[0], audio)})
        except transport.TransportError as error:
            raise TranscribeError(str(error)) from None
        text = str((reply or {}).get("text") or "").strip()
        return text

    return transcribe
