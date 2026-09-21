"""The two wire formats, and everything that differs between them.

FillerAI does not have an opinion about whose model proposes a form's rules.
It has an opinion about the *shape* of the question - a system prompt, a user
turn, and an answer in a named JSON schema - and both Anthropic's Messages API
and OpenAI's Chat Completions API can be asked that question. So the parts
that differ are gathered here and nowhere else:

* which environment variable holds the key, and how it is sent;
* which path on the host takes the request;
* what one call's body looks like;
* how to find the answer, the refusal and the truncation in the reply.

Everything above this module builds a request dictionary and reads a
:class:`Reply`, which is why adding the second provider changed the prompt, the
validation gate and the commands not at all.

Two differences are worth naming rather than leaving in the code.

**A refusal and a truncation both arrive as a successful call.** Each provider
says so in its own place - ``stop_reason`` against ``content``, or
``finish_reason`` against ``choices[0].message.refusal`` - and both are checked
before anything tries to parse the text, because both otherwise surface as a
confusing JSON error.

**Only one of the two enforces a schema for free.** Anthropic takes the schema
as given. OpenAI will *guarantee* the shape, but only for the subset of JSON
Schema that its strict mode accepts, which forbids the open-ended map that
``RULES_OUTPUT_SCHEMA`` needs for ``when``. So :func:`strict_ready` decides,
per schema, whether the guarantee is available, and the schema is sent either
way. Nothing downstream relies on the guarantee: a proposed rule is run
through the validation gate whoever wrote it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any

#: The name a structured answer is given. OpenAI requires one; Anthropic does
#: not take one. It appears in no prompt, so it is free to be descriptive.
SCHEMA_NAME = "fillerai_answer"


# --------------------------------------------------------------------------
# What a reply looks like once the provider's shape has been taken off it
# --------------------------------------------------------------------------


@dataclass
class Reply:
    """One answer, already parsed as far as it can be.

    ``usage`` is normalised onto ``input_tokens``/``output_tokens`` whatever
    the provider called them, and keeps the original keys alongside so a
    caller that wants the real numbers still has them.
    """

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


def strict_ready(schema: dict[str, Any]) -> bool:
    """Whether OpenAI's strict mode would accept this schema.

    Strict mode is all-or-nothing and its rules are narrow: every object has
    to forbid extra properties and require every property it declares. A map
    whose keys are not known in advance - which is exactly what a rule's
    ``when`` table is - cannot be expressed that way.

    Rather than bend the schema to fit, the schema is sent as it is and the
    guarantee is dropped. The answer is checked by the validation gate either
    way; a guarantee about its shape would save a parse error, not a bad rule.
    """
    if not isinstance(schema, dict):
        return True
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return False
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required") or ()) != set(properties):
            return False
        return all(strict_ready(child) for child in properties.values())
    if schema.get("type") == "array":
        return strict_ready(schema.get("items") or {})
    return True


def _parsed(reply: Reply, structured: bool) -> Reply:
    if structured:
        try:
            reply.data = json.loads(reply.text)
        except json.JSONDecodeError as error:
            raise ReplyError(f"the model's answer was not valid JSON: {error}") from None
    return reply


# --------------------------------------------------------------------------
# The providers themselves
# --------------------------------------------------------------------------


class Provider:
    """One service's dialect. Classes, not instances: there is no state."""

    #: What ``FILLERAI_LLM_PROVIDER`` is set to for this one.
    name = ""
    #: How a report should name it.
    label = ""
    #: The variable holding this provider's key, when the neutral
    #: ``FILLERAI_LLM_KEY`` is not set.
    key_variable = ""
    default_base_url = ""
    #: Appended to the base URL. Kept separate so a gateway can be pointed at
    #: with ``FILLERAI_LLM_BASE_URL`` and still get the right path.
    path = ""
    #: The importable name of the first-party SDK, used when it is installed.
    sdk_module = ""
    #: What this provider's own keys begin with. A last-resort guess, only
    #: consulted when nothing else says which provider is meant.
    key_prefix = ""
    #: Per task, because proposing a form's rules and reading a label are not
    #: the same problem. See :mod:`.config`.
    task_models: dict[str, str] = {}
    #: Prefixes of model names that belong to this provider.
    model_prefixes: tuple[str, ...] = ()

    @classmethod
    def recognises(cls, model: str) -> bool:
        return bool(model) and model.lower().startswith(cls.model_prefixes)

    @classmethod
    def headers(cls, key: str) -> dict[str, str]:
        raise NotImplementedError

    @classmethod
    def sdk_base_url(cls, base_url: str) -> str:
        """The base URL as this provider's SDK wants it written."""
        return base_url

    @classmethod
    def build(cls, *, model: str, system: str, user: str,
              output_schema: dict[str, Any] | None, max_tokens: int) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def read(cls, response: dict[str, Any], *, structured: bool = False) -> Reply:
        raise NotImplementedError

    @classmethod
    def answers(cls, response: dict[str, Any]) -> bool:
        """Whether a response looks like one of this provider's."""
        return False


class Anthropic(Provider):
    """The Messages API."""

    name = "anthropic"
    label = "Anthropic"
    key_variable = "ANTHROPIC_API_KEY"
    default_base_url = "https://api.anthropic.com"
    path = "/v1/messages"
    sdk_module = "anthropic"
    key_prefix = "sk-ant-"
    task_models = {"rules": "claude-opus-5", "typing": "claude-sonnet-5"}
    model_prefixes = ("claude",)

    #: The version header the Messages API requires on every request.
    api_version = "2023-06-01"

    @classmethod
    def headers(cls, key: str) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "anthropic-version": cls.api_version,
            "x-api-key": key,
        }

    @classmethod
    def build(cls, *, model: str, system: str, user: str,
              output_schema: dict[str, Any] | None, max_tokens: int) -> dict[str, Any]:
        """The system prompt is marked cacheable; it is identical across runs.

        Thinking is left unset on purpose: the models these tasks default to
        run it adaptively already, and naming it would only pin behaviour the
        service is better placed to choose.
        """
        request: dict[str, Any] = {
            "model": model,
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

    @classmethod
    def read(cls, response: dict[str, Any], *, structured: bool = False) -> Reply:
        stop = str(response.get("stop_reason") or "")
        if stop == "refusal":
            detail = (response.get("stop_details") or {}).get("explanation") or ""
            raise ReplyError(
                "the model declined to answer this request"
                + (f": {detail}" if detail else "")
            )
        if stop == "max_tokens":
            raise ReplyError(
                "the answer was cut off by the token limit; the form may be "
                "larger than this command's budget allows"
            )

        # The answer is the *last* text block, not the first: a response may
        # carry thinking blocks ahead of it, and content[0] works right up
        # until the day the model thinks about something.
        texts = [
            block.get("text", "")
            for block in response.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not texts:
            raise ReplyError(f"the model returned no text (stop_reason {stop!r})")

        return _parsed(Reply(
            text=texts[-1].strip(),
            model=str(response.get("model") or ""),
            stop_reason=stop,
            usage=dict(response.get("usage") or {}),
        ), structured)

    @classmethod
    def answers(cls, response: dict[str, Any]) -> bool:
        return "content" in response or "stop_reason" in response


class OpenAI(Provider):
    """Chat Completions."""

    name = "openai"
    label = "OpenAI"
    key_variable = "OPENAI_API_KEY"
    default_base_url = "https://api.openai.com"
    path = "/v1/chat/completions"
    sdk_module = "openai"
    key_prefix = "sk-"
    task_models = {"rules": "gpt-5", "typing": "gpt-5-mini"}
    model_prefixes = ("gpt-", "chatgpt", "o1", "o3", "o4")

    @classmethod
    def headers(cls, key: str) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
        }

    @classmethod
    def sdk_base_url(cls, base_url: str) -> str:
        """The SDK wants the ``/v1`` included; our base URL does not have it."""
        return base_url.rstrip("/") + "/v1"

    @classmethod
    def build(cls, *, model: str, system: str, user: str,
              output_schema: dict[str, Any] | None, max_tokens: int) -> dict[str, Any]:
        """``max_completion_tokens``, not ``max_tokens``.

        The older name is refused outright by every reasoning model, and the
        newer one is accepted by everything still worth pointing this at.
        Temperature is left unset for the same reason thinking is on the other
        side: some models take it, some reject it, and none of them need it
        told for a task that wants the most likely answer anyway.
        """
        request: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if output_schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "schema": output_schema,
                    "strict": strict_ready(output_schema),
                },
            }
        return request

    @classmethod
    def read(cls, response: dict[str, Any], *, structured: bool = False) -> Reply:
        choices = response.get("choices") or []
        if not choices:
            raise ReplyError("the model returned no choices at all")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        stop = str(choice.get("finish_reason") or "")

        refusal = message.get("refusal")
        if refusal:
            raise ReplyError(f"the model declined to answer this request: {refusal}")
        if stop == "length":
            raise ReplyError(
                "the answer was cut off by the token limit; the form may be "
                "larger than this command's budget allows"
            )

        text = (message.get("content") or "").strip()
        if not text:
            raise ReplyError(f"the model returned no text (finish_reason {stop!r})")

        usage = dict(response.get("usage") or {})
        usage.setdefault("input_tokens", usage.get("prompt_tokens") or 0)
        usage.setdefault("output_tokens", usage.get("completion_tokens") or 0)

        return _parsed(Reply(
            text=text,
            model=str(response.get("model") or ""),
            stop_reason=stop,
            usage=usage,
        ), structured)

    @classmethod
    def answers(cls, response: dict[str, Any]) -> bool:
        return "choices" in response


PROVIDERS: dict[str, type[Provider]] = {p.name: p for p in (Anthropic, OpenAI)}
NAMES = tuple(PROVIDERS)
DEFAULT = Anthropic.name


def get(name: str) -> type[Provider]:
    """The provider called ``name``, or a message naming the ones that exist."""
    try:
        return PROVIDERS[name.strip().lower()]
    except KeyError:
        known = ", ".join(NAMES)
        raise ValueError(
            f"no language-model provider called {name!r}; try one of {known}"
        ) from None


def for_model(model: str) -> type[Provider] | None:
    """Which provider owns a model name, when the name gives it away."""
    for provider in PROVIDERS.values():
        if provider.recognises(model):
            return provider
    return None


def for_response(response: dict[str, Any]) -> type[Provider]:
    """Which provider a response came from, judged by its shape.

    Only used when a reply arrives with no settings attached - replaying a
    recording, most of all, where the fixture is the only evidence of who
    answered.
    """
    for provider in PROVIDERS.values():
        if provider.answers(response):
            return provider
    return PROVIDERS[DEFAULT]


__all__ = [
    "Anthropic",
    "DEFAULT",
    "NAMES",
    "OpenAI",
    "PROVIDERS",
    "Provider",
    "Reply",
    "ReplyError",
    "SCHEMA_NAME",
    "for_model",
    "for_response",
    "get",
    "strict_ready",
]
