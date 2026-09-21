"""What the optional language-model features need to know, and where from.

Everything here is read from the environment, following the convention the
rest of the package already uses (``FILLERAI_HOME``,
``FILLERAI_DATABASE_URL``). Nothing is read from the library, nothing is
written to the database, and the key is never printed back: :meth:`Settings.
redacted` is the only way anything in this package will show it, and it shows
four characters.

Two settings are worth explaining rather than listing.

**The model is chosen per task, not once.** Proposing a form's business rules
is a reasoning problem solved once per form design, so it is worth the best
model available. Reading a label and deciding whether a field is a phone
number is fifty easy classifications, so it is not. A single default would
have to be wrong for one of them.

**The base URL exists so this can run somewhere else.** The whole reason
FillerAI generates its own data is that the real data cannot leave; a team
that wants these features and cannot send anything to a third party points
this at their own deployment - Bedrock, Vertex, Foundry or a gateway - and the
rest of the package neither knows nor cares.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The key, and the name everyone already has exported.
KEY_VARIABLE = "FILLERAI_LLM_KEY"
FALLBACK_KEY_VARIABLE = "ANTHROPIC_API_KEY"
#: Overrides whichever per-task default would otherwise apply.
MODEL_VARIABLE = "FILLERAI_LLM_MODEL"
#: Points the whole thing at a different deployment.
BASE_URL_VARIABLE = "FILLERAI_LLM_BASE_URL"

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

#: What each task uses when nothing says otherwise. See the module docstring
#: for why these are not the same model.
TASK_MODELS = {
    "rules": "claude-opus-5",
    "typing": "claude-sonnet-5",
}

TASKS = tuple(TASK_MODELS)


@dataclass(frozen=True)
class Settings:
    """Everything one call needs, resolved once so it can be reported."""

    task: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    key: str | None = None

    @classmethod
    def resolve(cls, task: str, *, model: str | None = None,
                environ: dict[str, str] | None = None) -> "Settings":
        """Read the environment for one task.

        ``environ`` is injectable so the tests do not have to mutate the
        process they run in.
        """
        if task not in TASK_MODELS:
            known = ", ".join(TASKS)
            raise ValueError(f"no language-model task called {task!r}; try one of {known}")
        env = os.environ if environ is None else environ
        return cls(
            task=task,
            model=model or env.get(MODEL_VARIABLE) or TASK_MODELS[task],
            base_url=(env.get(BASE_URL_VARIABLE) or DEFAULT_BASE_URL).rstrip("/"),
            key=env.get(KEY_VARIABLE) or env.get(FALLBACK_KEY_VARIABLE) or None,
        )

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/messages"

    def redacted(self) -> str:
        """The key as a report may show it, which is barely at all."""
        if not self.key:
            return "not set"
        return f"set (…{self.key[-4:]})" if len(self.key) > 4 else "set"

    def require_key(self) -> str:
        if not self.key:
            raise ConfigError(
                f"no API key: export ${KEY_VARIABLE} (or ${FALLBACK_KEY_VARIABLE}). "
                "Nothing else in FillerAI needs one."
            )
        return self.key


class ConfigError(RuntimeError):
    """Raised when a language-model feature was asked for without a key."""
