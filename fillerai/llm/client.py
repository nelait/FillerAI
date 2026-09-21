"""Building a request, and reading what comes back. No network in here.

The split between this and :mod:`.transport` is the whole point: everything
that decides *what* to ask lives here and is ordinary testable code, and
everything that actually sends it lives in one method somewhere else.

Two details about reading a response are worth stating, because both are easy
to get wrong and neither fails loudly.

**The answer is the last text block, not the first.** A response may carry
thinking blocks before the answer. Taking ``content[0]`` works right up until
the day the model thinks about something, and then returns an empty string.

**A refusal arrives as a successful response.** ``stop_reason`` of
``"refusal"`` comes back with HTTP 200 and no useful content, so it is checked
before the content is read rather than after it confuses a JSON parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any

from . import transport as transport_module
from .config import Settings
from .transport import Transport, TransportError


@dataclass
class Reply:
    """One answer, already parsed as far as it can be."""

    text: str
    model: str
    stop_reason: str
    usage: dict[str, Any] = dc_field(default_factory=dict)
    #: The parsed JSON, when the call asked for structured output.
    data: Any | None = None

    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("input_tokens") or 0)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("output_tokens") or 0)


class ReplyError(RuntimeError):
    """The far end answered, but not with anything usable."""


class Client:
    """Asks one question at a time."""

    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        # Built lazily: constructing a Client should not require a key, so
        # that `llm status` can report on one that is not configured.
        self._transport = transport

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = transport_module.for_settings(self.settings)
        return self._transport

    # -- building ----------------------------------------------------------

    def build(self, *, system: str, user: str,
              output_schema: dict[str, Any] | None = None,
              max_tokens: int = 8000) -> dict[str, Any]:
        """The request body, as a plain dictionary.

        The system prompt is marked cacheable because it is identical across
        runs and the user turn is not. Thinking is left unset on purpose: the
        models these tasks default to run it adaptively already, and naming it
        would only pin behaviour that the service is better placed to choose.
        """
        request: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": max_tokens,
            "system": [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user}],
        }
        if output_schema is not None:
            request["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }
        return request

    # -- asking ------------------------------------------------------------

    def ask(self, *, system: str, user: str,
            output_schema: dict[str, Any] | None = None,
            max_tokens: int = 8000) -> Reply:
        request = self.build(system=system, user=user,
                             output_schema=output_schema, max_tokens=max_tokens)
        return self.read(self.transport.send(request), structured=output_schema is not None)

    # -- reading -----------------------------------------------------------

    @staticmethod
    def read(response: dict[str, Any], *, structured: bool = False) -> Reply:
        stop = str(response.get("stop_reason") or "")
        if stop == "refusal":
            detail = (response.get("stop_details") or {}).get("explanation") or ""
            raise ReplyError(
                "the model declined to answer this request"
                + (f": {detail}" if detail else "")
            )

        texts = [
            block.get("text", "")
            for block in response.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not texts:
            raise ReplyError(
                f"the model returned no text (stop_reason {stop!r})"
            )
        text = texts[-1].strip()

        reply = Reply(
            text=text,
            model=str(response.get("model") or ""),
            stop_reason=stop,
            usage=dict(response.get("usage") or {}),
        )

        if stop == "max_tokens":
            raise ReplyError(
                "the answer was cut off by the token limit; the form may be "
                "larger than this command's budget allows"
            )

        if structured:
            try:
                reply.data = json.loads(text)
            except json.JSONDecodeError as error:
                raise ReplyError(f"the model's answer was not valid JSON: {error}") from None
        return reply


__all__ = ["Client", "Reply", "ReplyError", "TransportError"]
