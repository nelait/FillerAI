"""The optional language-model features, fenced off from everything else.

**Nothing under ``fillerai/`` imports this package at module scope**, and
``tests/test_llm_fence.py`` fails if that ever stops being true. The two
commands that use it import it inside the function that runs them.

That is stricter than "an optional dependency", on purpose. The README
promises that nothing here talks to a network, and that promise is what lets
FillerAI run inside the locked-down environment where the real form lives. One
import in the wrong place turns an optional feature into a mandatory one
without anybody noticing, so the fence is a test rather than a convention.

Everything here is also still dependency-free: :class:`~.transport.
UrllibTransport` calls the Messages API with ``urllib.request`` and ``json``,
because that is all an HTTPS JSON endpoint needs. The ``anthropic`` SDK is
used automatically when it happens to be installed and is required by nothing
in phase 1.

What is here:

``config``     where the key and the model come from, and how not to print them
``transport``  the only module that opens a socket - and its recorded stand-in
``client``     building a request, reading a reply; no network
``prompts``    what gets asked, and the shape of the answer
``cost``       what a call will cost, said before it is made
``rules``      proposing a form's own business rules, and disbelieving them
"""

from __future__ import annotations

from . import client, config, cost, prompts, rules, transport
from .client import Client, Reply, ReplyError
from .config import ConfigError, Settings
from .cost import Estimate, SpendRefused
from .transport import (
    RecordedTransport,
    SdkTransport,
    Transport,
    TransportError,
    UrllibTransport,
)

__all__ = [
    "Client",
    "ConfigError",
    "Estimate",
    "RecordedTransport",
    "Reply",
    "ReplyError",
    "SdkTransport",
    "Settings",
    "SpendRefused",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "client",
    "config",
    "cost",
    "prompts",
    "rules",
    "transport",
]
