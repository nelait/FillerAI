"""The bot service on the ``/v1`` surface: templates, and one turn at a time.

Same promises as the rest of :mod:`.rest` - bearer tokens only, a stable
``code`` on every refusal, addressed by a name that survives a restart (here
a template's ``key``) - and, like it, no logic of its own: the conversation is
:func:`fillerai.bot.take_turn`, which the UI's Bots panel calls too.

The contract is ``docs/bot-builder.md``.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Callable

from ..bot import BotError, Reader, TemplateError, model_completer, take_turn
from ..bot import template as templates_module
from ..bot.template import Template
from ..store import StoreError
from . import rest
from .rest import Caller, Endpoint, RestError

#: Whose reader to use for a turn, given the library owner's id. The server
#: replaces this when it is started with the language-model reader turned
#: on; left alone, every turn is read on this machine.
READER: Callable[[str], Reader | None] = lambda owner: None

#: Whose transcriber to use for a recording, given the library owner's id: a
#: function ``(audio, mime, language) -> text``, or None when the server was
#: not started with ``--bot-transcribe``. Speech is then the browser's job.
TRANSCRIBER: Callable[[str], Callable[[bytes, str, str], str] | None] = lambda owner: None

#: Base64 of the largest recording accepted, with room to spare.
MAX_AUDIO_FIELD = 6 * 1024 * 1024

_KEY = r"(?P<key>[a-z][a-z0-9_]{0,63})"


def _scope(caller: Caller) -> str | None:
    return caller.token.model_id if caller.token else None


def _allowed(caller: Caller, template: Template) -> bool:
    """A token pinned to a model reaches only the templates linked to it."""
    scope = _scope(caller)
    return scope is None or template.model_id == scope


def _available(caller: Caller) -> list[Template]:
    return [t for t in templates_module.listed(caller.store) if _allowed(caller, t)]


def _find(caller: Caller, key: str) -> Template:
    template = templates_module.find(caller.store, key)
    if template is None:
        raise RestError(f"there is no template called {key!r}", status=404,
                        code="unknown_template")
    if not _allowed(caller, template):
        raise RestError("this token is issued for a different model", status=403,
                        code="out_of_scope")
    return template


def _may_write(caller: Caller) -> None:
    if _scope(caller) is not None:
        raise RestError("a token pinned to one model cannot change templates",
                        status=403, code="out_of_scope")


# ----------------------------------------------------------------------
# the endpoints
# ----------------------------------------------------------------------


def templates(caller: Caller, params: dict[str, str],
              body: dict[str, Any]) -> dict[str, Any]:
    found = _available(caller)
    return {"templates": [t.card() for t in found], "count": len(found)}


def template(caller: Caller, params: dict[str, str],
             body: dict[str, Any]) -> dict[str, Any]:
    found = _find(caller, params["key"])
    return {"template": found.to_dict(), "entry_id": found.entry_id}


def save_template(caller: Caller, params: dict[str, str],
                  body: dict[str, Any]) -> dict[str, Any]:
    _may_write(caller)
    given = body.get("template", body)
    try:
        parsed = Template.from_dict(given)
    except TemplateError as error:
        raise RestError(str(error), code="bad_template") from None
    if parsed.model_id:
        # Linking a model this library does not have would only fail later,
        # on some end user's turn, where nobody can do anything about it.
        rest.load(caller, parsed.model_id)
    try:
        saved = templates_module.save(caller.store, parsed)
    except StoreError as error:
        raise RestError(str(error), code="bad_template") from None
    return {"template": saved.to_dict(), "entry_id": saved.entry_id}


def delete_template(caller: Caller, params: dict[str, str],
                    body: dict[str, Any]) -> dict[str, Any]:
    _may_write(caller)
    _find(caller, params["key"])
    gone = templates_module.remove(caller.store, params["key"])
    return {"deleted": params["key"], "entries": gone}


def turn(caller: Caller, params: dict[str, str],
         body: dict[str, Any]) -> dict[str, Any]:
    """One turn of a conversation. See ``docs/bot-builder.md`` §3."""
    return run_turn(caller.store, _available(caller), body,
                    load_model=lambda model_id: rest.load(caller, model_id)[0],
                    reader=READER(str(getattr(caller.store, "owner", "") or "")))


def run_turn(store: Any, available: list[Template], body: dict[str, Any], *,
             load_model: Callable[[str], Any], reader: Reader | None) -> dict[str, Any]:
    """A turn over some set of templates, shared by ``/v1`` and the UI."""
    context = body.get("context") or {}
    wanted = context.get("templates") if isinstance(context, dict) else None
    if wanted is not None:
        if not isinstance(wanted, list) or not all(isinstance(k, str) for k in wanted):
            raise RestError("context.templates must be a list of template keys",
                            code="bad_input")
        have = {t.key for t in available}
        unknown = [k for k in wanted if k not in have]
        if unknown:
            raise RestError(f"there is no template called {unknown[0]!r}", status=404,
                            code="unknown_template")
        available = [t for t in available if t.key in set(wanted)]
    try:
        return take_turn(available, body, reader=reader,
                         complete=model_completer(load_model))
    except BotError as error:
        raise RestError(error.message, status=error.status, code=error.code) from None


def transcribe(caller: Caller, params: dict[str, str],
               body: dict[str, Any]) -> dict[str, Any]:
    """A recording made in the browser, as text. See ``docs/bot-builder.md`` §7."""
    return run_transcribe(TRANSCRIBER(str(getattr(caller.store, "owner", "") or "")), body)


def run_transcribe(transcriber: Callable[[bytes, str, str], str] | None,
                   body: dict[str, Any]) -> dict[str, Any]:
    """Check a recording and hand it over; shared by ``/v1`` and the UI."""
    if transcriber is None:
        raise RestError("this server does not transcribe speech; start it with "
                        "--bot-transcribe, or use the browser's recogniser",
                        status=409, code="transcription_off")
    audio, mime = body.get("audio"), body.get("mime")
    if not isinstance(audio, str) or not audio or len(audio) > MAX_AUDIO_FIELD:
        raise RestError("audio must be the recording as base64, at most about 4 MB",
                        code="bad_input")
    if not isinstance(mime, str) or not mime.startswith("audio/"):
        raise RestError("mime must be the recording's type, such as audio/webm",
                        code="bad_input")
    try:
        data = base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError):
        raise RestError("audio is not valid base64", code="bad_input") from None
    language = body.get("language") if isinstance(body.get("language"), str) else ""
    # Imported here: the error type lives behind the language-model fence.
    from ..llm.transcribe import TranscribeError

    try:
        text = transcriber(data, mime, language)
    except TranscribeError as error:
        raise RestError(str(error), status=502, code="transcription_failed") from None
    return {"text": text, "via": "speech"}


ENDPOINTS: list[Endpoint] = [
    Endpoint("GET", re.compile(r"^/v1/templates$"), templates),
    Endpoint("POST", re.compile(r"^/v1/templates$"), save_template),
    Endpoint("GET", re.compile(rf"^/v1/templates/{_KEY}$"), template),
    Endpoint("POST", re.compile(rf"^/v1/templates/{_KEY}/delete$"), delete_template),
    Endpoint("POST", re.compile(r"^/v1/bot/turn$"), turn),
    Endpoint("POST", re.compile(r"^/v1/bot/transcribe$"), transcribe),
]
