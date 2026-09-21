"""The only module in FillerAI that opens a socket.

That is the entire design. Everything above this file builds a request
dictionary and reads a response dictionary, which makes every part of these
features testable without a network, and makes the one dangerous thing in the
package a single method on a single object that a test can replace.

Three kinds of implementation, for three situations:

:class:`UrllibTransport`
    The default, and provider-neutral: both services are an HTTPS endpoint
    that takes JSON and returns JSON, and they differ only in the headers and
    the path, which :mod:`.providers` supplies. This is what keeps
    ``dependencies = []`` true in ``pyproject.toml``. It gives up streaming
    and typed errors, neither of which matters for a single call made by a
    person at a terminal.

:class:`SdkTransport` and :class:`OpenAiSdkTransport`
    Used automatically when the matching first-party package happens to be
    installed - and only when it matches, because having ``anthropic`` on the
    path is no reason to send an OpenAI request through it. Worth having
    because retry, backoff and error classification are somebody else's
    problem there, and worth *requiring* for anything that makes thousands of
    calls rather than one.

:class:`RecordedTransport`
    Replays a recorded exchange and raises on a request it has never seen.
    Every test in this package uses it, which is how a non-deterministic
    feature sits inside a suite that is entirely deterministic. The raise
    matters as much as the replay: a changed prompt fails loudly here instead
    of quietly reaching for the network.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings

#: Status codes worth trying again. 429 is rate limiting; 5xx is the far end
#: having a bad moment. Everything else is a request that will fail the same
#: way however many times it is sent.
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """A call could not be completed.

    Carries ``status`` when the far end answered with one, so a caller can
    tell "your key is wrong" from "the network is down" without parsing a
    message.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def digest(request: dict[str, Any]) -> str:
    """A stable fingerprint of a request, for matching recordings.

    Sorted keys, so a dictionary that means the same thing fingerprints the
    same way whichever order it was built in.
    """
    blob = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Transport:
    """Turn a request dictionary into a response dictionary, somehow."""

    #: What a report should call this. Shown by ``fillerai llm status``.
    label = "none"

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class UrllibTransport(Transport):
    """The standard library, and nothing else."""

    label = "urllib (standard library)"

    def __init__(self, settings: Settings, *, timeout: float = 120.0,
                 retries: int = 3, sleep=time.sleep) -> None:
        self.settings = settings
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep  # injectable so a test does not actually wait

    def _request(self, body: bytes) -> urllib.request.Request:
        return urllib.request.Request(
            self.settings.endpoint,
            data=body,
            method="POST",
            headers=self.settings.api.headers(self.settings.require_key()),
        )

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(request).encode("utf-8")
        last = "no attempt was made"
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(
                    self._request(body), timeout=self.timeout
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = _detail(error)
                if error.code not in RETRY_STATUS or attempt == self.retries:
                    raise TransportError(
                        f"the model service answered {error.code}: {detail}",
                        status=error.code,
                    ) from None
                last = f"{error.code}: {detail}"
            except urllib.error.URLError as error:
                if attempt == self.retries:
                    raise TransportError(f"could not reach the model service: {error.reason}") from None
                last = str(error.reason)
            # Exponential, with jitter so several runs do not retry in step.
            self._sleep(min(2.0 ** attempt, 8.0) + random.random())
        raise TransportError(f"gave up after {self.retries + 1} attempts: {last}")


def _detail(error: urllib.error.HTTPError) -> str:
    """The message the far end sent, if it sent one we can read.

    Both providers wrap a failure the same way - ``{"error": {"message": …}}``
    - so one reader does for both.
    """
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return error.reason or "no detail"
    problem = payload.get("error") or {}
    return problem.get("message") or error.reason or "no detail"


class SdkTransport(Transport):
    """The ``anthropic`` package, when it is there."""

    label = "anthropic SDK"
    provider = "anthropic"
    module = "anthropic"

    def __init__(self, settings: Settings) -> None:
        import anthropic  # deliberately local: only built when available

        self.settings = settings
        self._client = anthropic.Anthropic(
            api_key=settings.require_key(),
            base_url=_sdk_base_url(settings),
        )

    @classmethod
    def available(cls) -> bool:
        return _importable(cls.module)

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            message = self._client.messages.create(**request)
        except Exception as error:  # the SDK's hierarchy is not ours to depend on
            raise TransportError(f"the model service refused the call: {error}") from None
        return message.to_dict() if hasattr(message, "to_dict") else dict(message)


class OpenAiSdkTransport(Transport):
    """The ``openai`` package, when it is there."""

    label = "openai SDK"
    provider = "openai"
    module = "openai"

    def __init__(self, settings: Settings) -> None:
        import openai  # deliberately local: only built when available

        self.settings = settings
        self._client = openai.OpenAI(
            api_key=settings.require_key(),
            base_url=_sdk_base_url(settings),
        )

    @classmethod
    def available(cls) -> bool:
        return _importable(cls.module)

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            completion = self._client.chat.completions.create(**request)
        except Exception as error:  # as above: not our exception hierarchy
            raise TransportError(f"the model service refused the call: {error}") from None
        if hasattr(completion, "model_dump"):
            return completion.model_dump()
        return dict(completion)


#: Which SDK speaks for which provider. Consulted by :func:`sdk_for`, which is
#: the only thing standing between an installed ``anthropic`` and an OpenAI
#: request being handed to it.
SDKS: dict[str, type[Transport]] = {
    SdkTransport.provider: SdkTransport,
    OpenAiSdkTransport.provider: OpenAiSdkTransport,
}


def _importable(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _sdk_base_url(settings: Settings) -> str | None:
    """Only override the SDK's own default when we have been pointed elsewhere.

    An SDK knows its own host better than we do; what it cannot know is that
    somebody set ``FILLERAI_LLM_BASE_URL`` to a gateway.
    """
    if not settings.custom_base_url:
        return None
    return settings.api.sdk_base_url(settings.base_url)


def sdk_for(settings: Settings) -> type[Transport] | None:
    """The first-party SDK for this provider, if it is installed."""
    sdk = SDKS.get(settings.provider)
    return sdk if sdk is not None and sdk.available() else None


class RecordedTransport(Transport):
    """Replay. Raises on anything it was not given an answer for.

    A recording either names the ``digest`` of the request it answers, in
    which case it is matched exactly, or does not, in which case it is handed
    out in order. The first is what ``--record`` writes; the second is what
    makes a fixture readable enough to write by hand.
    """

    label = "recorded (no network)"

    def __init__(self, recordings: list[dict[str, Any]]) -> None:
        self._keyed = {r["digest"]: r["response"] for r in recordings if r.get("digest")}
        self._ordered = [r["response"] for r in recordings if not r.get("digest")]
        self._used = 0
        #: Every request that was sent, for a test to assert about.
        self.sent: list[dict[str, Any]] = []

    @classmethod
    def from_path(cls, path) -> "RecordedTransport":
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["recordings"])

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(request)
        key = digest(request)
        if key in self._keyed:
            return self._keyed[key]
        if self._used < len(self._ordered):
            self._used += 1
            return self._ordered[self._used - 1]
        raise TransportError(
            f"no recording for this request (digest {key}). The prompt has "
            "changed since the fixture was recorded - re-record it rather "
            "than reaching for the network."
        )


def for_settings(settings: Settings) -> Transport:
    """The transport to use, given the provider and what is installed."""
    sdk = sdk_for(settings)
    if sdk is not None:
        return sdk(settings)
    return UrllibTransport(settings)
