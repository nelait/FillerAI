"""Reading a chat phrase with a language model, for the bot service.

The local reading in :mod:`fillerai.bot.understand` finds a field's words and
takes what follows them, which is right for "change my city to Irvine" and
helpless for "we're moving in with my sister in Tustin next month". A model
reads the second one. This module asks it for exactly what the local reading
produces - the template meant and the values said - and nothing more.

**Off unless turned on, separately from having a key.** Proposing rules sends
a form's field names; this sends what an end user typed, which is their data.
So the service uses this only when started with ``--bot-llm`` (or
``FILLERAI_BOT_LLM=1``), and a key being present is not enough.

**Believed only as far as it checks out.** The answer names a template, which
must be one this chat may use; its values name fields, which must be that
template's; and every value goes through :func:`fillerai.bot.understand.accept`,
the same gate a typed value does, so a model cannot put "Mars" in a state or
an option that is not on the list. Anything that fails is dropped, and any
failure at all - no key, a refused call, a malformed answer - falls back to
the local reading for that turn rather than failing it.
"""

from __future__ import annotations

import json
from typing import Any

from ..bot import understand as local
from ..bot.template import Template
from .client import Client
from .config import Settings

#: Enough for a template and a dozen values, with room for a model that
#: thinks before it answers.
CHAT_BUDGET = 2000
#: Examples shown per template. The local reader uses all of them; a model
#: needs a flavour, not the list.
EXAMPLES_SHOWN = 5

SYSTEM = """You read one message from a person talking to a customer-service chat \
and say which request template it is about and which field values it states.

Rules:
- template: the key of the template the message is about, or null if it is not \
clearly about any of them. If a template is already in progress and the message \
just adds or corrects information, answer with that template.
- values: only what the person actually said. Never invent, look up or infer a \
value they did not state. Use the field names given, exactly.
- If the person says an old value and a new one ("from SFO to Irvine"), the \
value is the new one and said_before is the old one; otherwise said_before is "".
- For a field with options, the value must be one of the options, written as listed.
- If a question was just asked about a field and the message is only an answer, \
that answer is the value of that field.
- confidence: how sure you are about the template, from 0 to 1."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["template", "confidence", "values"],
    "properties": {
        "template": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "value", "said_before"],
                "properties": {
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "said_before": {"type": "string"},
                },
            },
        },
    },
}


def describe(templates: list[Template]) -> list[dict[str, Any]]:
    """The templates as the model sees them: names and fields, no data."""
    out = []
    for t in templates:
        fields = []
        for f in t.fields:
            row: dict[str, Any] = {"name": f.name, "label": f.label,
                                   "type": f.semantic_type}
            if f.options:
                row["options"] = f.options[:40]
            fields.append(row)
        out.append({"key": t.key, "name": t.name, "description": t.description,
                    "examples": t.examples[:EXAMPLES_SHOWN], "fields": fields})
    return out


def user_prompt(templates: list[Template], active: Template | None, text: str, *,
                expects: str | None = None, last_field: str | None = None) -> str:
    return json.dumps({
        "templates": describe(templates),
        "in_progress": active.key if active else None,
        "just_asked_about": expects,
        "last_field_mentioned": last_field,
        "message": text,
    }, ensure_ascii=False, indent=1)


def read_answer(payload: Any, templates: list[Template],
                active: Template | None) -> local.Reading:
    """A model's answer as a :class:`Reading`, with everything it cannot back up dropped."""
    if not isinstance(payload, dict):
        raise ValueError("the answer is not an object")
    by_key = {t.key: t for t in templates}
    key = payload.get("template")
    key = str(key) if key in by_key else None
    target = by_key.get(key) if key else active
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    reading = local.Reading(how="llm", template=key)
    scored = {t.key: 0.0 for t in templates}
    if key:
        scored[key] = confidence
    reading.scores = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    if target is None:
        return reading

    for item in payload.get("values") or []:
        if not isinstance(item, dict):
            continue
        field = target.field(str(item.get("field") or ""))
        if field is None:
            continue
        value, reason = local.accept(field, str(item.get("value") or ""))
        if value is None:
            reading.problems.append(local.Problem(field.name, str(item.get("value")), reason))
            continue
        reading.values[field.name] = local.Found(
            value=value, said_before=str(item.get("said_before") or "")[:200],
            how="llm", confidence=0.85)
    return reading


def reader(settings: Settings, *, client: Client | None = None):
    """A reader for :func:`fillerai.bot.take_turn` that asks a model first."""
    asker = client or Client(settings)

    def read(templates: list[Template], active: Template | None, text: str, *,
             expects: str | None = None, last_field: str | None = None) -> local.Reading:
        try:
            settings.require_key()
            reply = asker.ask(system=SYSTEM,
                              user=user_prompt(templates, active, text, expects=expects,
                                               last_field=last_field),
                              output_schema=OUTPUT_SCHEMA, max_tokens=CHAT_BUDGET)
            reading = read_answer(reply.data, templates, active)
        except Exception:  # noqa: BLE001 - any failure is a turn read locally instead
            return local.read(templates, active, text, expects=expects,
                              last_field=last_field)
        if reading.template is None and not reading.values:
            # The model saw nothing; the local reader may still see a ZIP code.
            fallback = local.read(templates, active, text, expects=expects,
                                  last_field=last_field)
            if fallback.values or local.decide(fallback.scores)[0]:
                return fallback
        return reading

    return read
