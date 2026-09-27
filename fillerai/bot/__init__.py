"""Bot Builder: templates, and a chat bot that fills them in from a conversation.

``template``      what one kind of request is made of, and keeping it in the library
``understand``    reading a phrase on this machine: which template, which values
``conversation``  one turn, as a pure function of the input and the state

The optional language-model reading lives in :mod:`fillerai.llm.understand`,
behind the same import fence as everything else there: nothing in this
package imports it, and the service decides at the call whether to use it.

The contract with a chat window in another application is written down in
``docs/bot-builder.md``.
"""

from __future__ import annotations

from typing import Any, Callable

from .conversation import BotError, Completer, Reader, take_turn
from .template import Template, TemplateError, TemplateField, from_schema
from .understand import Found, Problem, Reading


def model_completer(load: Callable[[str], Any], threshold: float | None = None) -> Completer:
    """A completer that asks a template's linked autofill model.

    ``load`` turns a model id into a trained model; the web service passes
    the same cached loader ``/v1/models`` uses. Only answers at or above the
    model's calibrated threshold are offered, and only for fields the
    template has, so the model can finish a form but never widen it.
    """
    from ..train.model import ACCEPT_ABOVE

    bar = ACCEPT_ABOVE if threshold is None else threshold

    def complete(template: Template, observed: dict[str, str]) -> dict[str, tuple[str, float]]:
        if not template.model_id or not observed:
            return {}
        model = load(template.model_id)
        known = {k: v for k, v in observed.items() if k in model.profiles}
        if not known:
            return {}
        wanted = set(template.names())
        out: dict[str, tuple[str, float]] = {}
        for name, guess in model.predict(known).items():
            if name in wanted and guess.known and guess.value and guess.confidence >= bar:
                out[name] = (str(guess.value), float(guess.confidence))
        return out

    return complete


__all__ = [
    "BotError",
    "Completer",
    "Found",
    "Problem",
    "Reader",
    "Reading",
    "Template",
    "TemplateError",
    "TemplateField",
    "from_schema",
    "model_completer",
    "take_turn",
]
