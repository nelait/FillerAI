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
UrllibTransport` calls either service with ``urllib.request`` and ``json``,
because that is all an HTTPS JSON endpoint needs. A first-party SDK
(``anthropic`` or ``openai``) is used automatically when it happens to be
installed and matches the provider in use, and is required by nothing here.

What is here:

``providers``  the two wire formats, and everything that differs between them
``config``     where the key and the model come from, and how not to print them
``transport``  the only module that opens a socket - and its recorded stand-in
``client``     building a request, reading a reply; no network
``prompts``    what gets asked, and the shape of the answer
``cost``       what a call will cost, said before it is made
``rules``      proposing a form's own business rules, and disbelieving them
"""

from __future__ import annotations

from . import client, config, cost, prompts, providers, rules, transport
from .client import Client, Reply, ReplyError
from .config import ConfigError, Settings
from .cost import Estimate, SpendRefused
from .providers import Provider, strict_ready
from .transport import (
    OpenAiSdkTransport,
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
    "OpenAiSdkTransport",
    "Provider",
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
    "providers",
    "rules",
    "strict_ready",
    "transport",
]
