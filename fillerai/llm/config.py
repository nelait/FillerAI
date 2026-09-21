"""What the optional language-model features need to know, and where from.

Everything here is read from the environment, following the convention the
rest of the package already uses (``FILLERAI_HOME``,
``FILLERAI_DATABASE_URL``). Nothing is read from the library, nothing is
written to the database, and the key is never printed back: :meth:`Settings.
redacted` is the only way anything in this package will show it, and it shows
four characters.

Three settings are worth explaining rather than listing.

**The provider is worked out, not demanded.** Somebody who has exported
``OPENAI_API_KEY`` has already said which service they mean, and being made to
say it twice would be a papercut on the first command they ever run. So
:func:`choose_provider` reads the evidence in order - an explicit choice, then
``FILLERAI_LLM_PROVIDER``, then a model name that gives itself away, then which
provider's key variable is set, then what the neutral key looks like - and
stops at the first thing that answers. It also records *which* of those
answered, because a guess that cannot explain itself is worse than a prompt:
``fillerai llm status`` prints the reason next to the provider.

**The model is chosen per task, not once.** Proposing a form's business rules
is a reasoning problem solved once per form design, so it is worth the best
model available. Reading a label and deciding whether a field is a phone
number is fifty easy classifications, so it is not. A single default would
have to be wrong for one of them - and the pair differs by provider, which is
why the defaults live on the provider rather than here.

**The base URL exists so this can run somewhere else.** The whole reason
FillerAI generates its own data is that the real data cannot leave; a team
that wants these features and cannot send anything to a third party points
this at their own deployment - Bedrock, Vertex, Foundry, Azure or a gateway -
and the rest of the package neither knows nor cares.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import providers
from .providers import Provider

#: The key, whichever provider is in use. Set this and nothing else has to be.
KEY_VARIABLE = "FILLERAI_LLM_KEY"
#: Which service to talk to: ``anthropic`` or ``openai``.
PROVIDER_VARIABLE = "FILLERAI_LLM_PROVIDER"
#: Overrides whichever per-task default would otherwise apply.
MODEL_VARIABLE = "FILLERAI_LLM_MODEL"
#: Points the whole thing at a different deployment.
BASE_URL_VARIABLE = "FILLERAI_LLM_BASE_URL"

TASKS = ("rules", "typing")


def choose_provider(explicit: str | None, model: str | None,
                    env: dict[str, str]) -> tuple[type[Provider], str]:
    """Which service is meant, and what said so.

    The reason is returned rather than logged because it is the difference
    between "it used the wrong key" and "it used the wrong key *because* you
    still have an old ``ANTHROPIC_API_KEY`` exported".
    """
    if explicit:
        return providers.get(explicit), "asked for on the command line"

    named = env.get(PROVIDER_VARIABLE)
    if named:
        return providers.get(named), f"${PROVIDER_VARIABLE}"

    wanted = model or env.get(MODEL_VARIABLE)
    if wanted:
        owner = providers.for_model(wanted)
        if owner is not None:
            return owner, f"the model name {wanted}"

    # Exactly one provider's own key variable being set is a clear answer.
    # Two of them set is not, so it falls through rather than picking.
    have = [p for p in providers.PROVIDERS.values() if env.get(p.key_variable)]
    if len(have) == 1:
        return have[0], f"${have[0].key_variable} is set"

    key = env.get(KEY_VARIABLE)
    if key:
        # Longest prefix first, so sk-ant- beats sk-.
        for provider in sorted(providers.PROVIDERS.values(),
                               key=lambda p: -len(p.key_prefix)):
            if provider.key_prefix and key.startswith(provider.key_prefix):
                return provider, f"${KEY_VARIABLE} begins {provider.key_prefix}"

    default = providers.get(providers.DEFAULT)
    if len(have) > 1:
        # Say what the ambiguity was. Somebody with two keys exported and no
        # preference stated is one command away from wondering why the wrong
        # one was billed.
        exported = ", ".join(f"${p.key_variable}" for p in have)
        return default, f"the default; {exported} are both set, so neither decided it"
    return default, "the default"


@dataclass(frozen=True)
class Settings:
    """Everything one call needs, resolved once so it can be reported."""

    task: str
    provider: str
    model: str
    base_url: str
    key: str | None = None
    #: What chose the provider. For reporting; nothing branches on it.
    provider_reason: str = ""

    @classmethod
    def resolve(cls, task: str, *, model: str | None = None,
                provider: str | None = None,
                environ: dict[str, str] | None = None) -> "Settings":
        """Read the environment for one task.

        ``environ`` is injectable so the tests do not have to mutate the
        process they run in.
        """
        if task not in TASKS:
            known = ", ".join(TASKS)
            raise ValueError(f"no language-model task called {task!r}; try one of {known}")
        env = os.environ if environ is None else environ
        chosen, reason = choose_provider(provider, model, env)
        return cls(
            task=task,
            provider=chosen.name,
            model=model or env.get(MODEL_VARIABLE) or chosen.task_models[task],
            base_url=(env.get(BASE_URL_VARIABLE) or chosen.default_base_url).rstrip("/"),
            key=env.get(KEY_VARIABLE) or env.get(chosen.key_variable) or None,
            provider_reason=reason,
        )

    @property
    def api(self) -> type[Provider]:
        """The provider's dialect: headers, request shape, reply shape."""
        return providers.get(self.provider)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{self.api.path}"

    @property
    def custom_base_url(self) -> bool:
        """Whether this is pointed somewhere other than the provider's own host."""
        return self.base_url != self.api.default_base_url.rstrip("/")

    def redacted(self) -> str:
        """The key as a report may show it, which is barely at all."""
        if not self.key:
            return "not set"
        return f"set (…{self.key[-4:]})" if len(self.key) > 4 else "set"

    def require_key(self) -> str:
        if not self.key:
            raise ConfigError(
                f"no API key for {self.api.label}: export ${KEY_VARIABLE} "
                f"(or ${self.api.key_variable}). Nothing else in FillerAI needs one."
            )
        return self.key


class ConfigError(RuntimeError):
    """Raised when a language-model feature was asked for without a key."""
