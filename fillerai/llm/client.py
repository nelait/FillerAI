"""Building a request, and reading what comes back. No network in here.

The split between this and :mod:`.transport` is the whole point: everything
that decides *what* to ask lives here and is ordinary testable code, and
everything that actually sends it lives in one method somewhere else. What the
request and the reply look like on the wire is :mod:`.providers`' business,
which is why this file has no provider's name in it.

:meth:`Client.read` will read a reply with nobody having told it who wrote it,
by looking at the shape. That is not cleverness for its own sake: a recorded
fixture is a response and nothing else, and a recording made against one
service should still replay when the settings say another.
"""

from __future__ import annotations

from typing import Any

from . import providers
from . import transport as transport_module
from .config import Settings
from .providers import Reply, ReplyError
from .transport import Transport, TransportError


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
        """The request body for this provider, as a plain dictionary."""
        return self.settings.api.build(
            model=self.settings.model, system=system, user=user,
            output_schema=output_schema, max_tokens=max_tokens,
        )

    # -- asking ------------------------------------------------------------

    def ask(self, *, system: str, user: str,
            output_schema: dict[str, Any] | None = None,
            max_tokens: int = 8000) -> Reply:
        request = self.build(system=system, user=user,
                             output_schema=output_schema, max_tokens=max_tokens)
        response = self.transport.send(request)
        return self.read(response, structured=output_schema is not None)

    # -- reading -----------------------------------------------------------

    @staticmethod
    def read(response: dict[str, Any], *, structured: bool = False,
             provider: str | None = None) -> Reply:
        """Turn one response into a :class:`~.providers.Reply`.

        With no ``provider``, the response's own shape decides which reader
        runs - see the module docstring.
        """
        api = (providers.get(provider) if provider
               else providers.for_response(response))
        return api.read(response, structured=structured)


__all__ = ["Client", "Reply", "ReplyError", "TransportError"]
