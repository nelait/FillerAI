"""What gets asked, and the shape the answer has to come back in.

Kept in one module away from the code that uses it for a practical reason:
the recorded fixtures the tests run against are keyed on the exact bytes of a
request, so a prompt edit breaks them loudly. Having every prompt in one file
makes that boundary visible rather than scattered through the features.

The rules prompt quotes this project's own documentation rather than
paraphrasing it. ``Derived`` already explains what a declared rule is, and
``spec`` already explains how one is written; restating either in a prompt
would be a second definition to keep in step with the first.
"""

from __future__ import annotations

from typing import Any

from ..schema import FormSchema

# --------------------------------------------------------------------------
# Rendering a form for a model to read
# --------------------------------------------------------------------------

#: Past this many, a field's options are summarised rather than listed. A
#: state dropdown is fifty values and listing them teaches nothing a model
#: does not know; a coverage tier is four and they are the whole point.
MAX_OPTIONS_SHOWN = 24


def compact_schema(schema: FormSchema) -> str:
    """A form as a model should read it: one line per field, no JSON.

    Deliberately not ``schema.to_dict()``. The JSON is three times the size,
    and most of what it carries - confidence, evidence, data types, the
    inference audit trail - is this package talking to itself.
    """
    lines = []
    for field in schema.fields:
        parts = [
            field.name,
            field.label or "",
            field.semantic_type,
            field.control,
        ]
        if field.group:
            parts.append(f"group={field.group}")
        if field.options:
            values = [o.value for o in field.options]
            if len(values) > MAX_OPTIONS_SHOWN:
                shown = ", ".join(values[:MAX_OPTIONS_SHOWN])
                parts.append(f"options({len(values)})={shown}, …")
            else:
                parts.append("options=" + " | ".join(values))
        if field.derived:
            parts.append("already has a rule")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Phase 1: proposing the rules a form declares about itself
# --------------------------------------------------------------------------

RULES_SYSTEM = """\
You are helping build synthetic sample data for a web form. The people who \
own the real form cannot share their real submissions, so the data is \
generated - and generated data is only useful if the relationships between \
fields are the ones the real form actually enforces.

Your job is to propose those relationships, from the form's field list alone.

A rule says that one field FOLLOWS one or more others: its value is decided, \
or narrowed, by theirs. Two kinds are real and both are wanted:

  - a determination - "Platinum means a 250 deductible"
  - a narrowing - "Engineering means one of these four job titles"

A rule may follow several fields at once, when the relationship lives in the \
combination rather than in either field alone - a premium band set by \
coverage tier AND vehicle use, say. Those are the most valuable rules to \
find, because they are the ones a simple counting model cannot learn.

Propose a rule only when you are confident the real form enforces it, for one \
of two reasons:

  1. It is a fact about the world. A Wrangler is made by Jeep. The ZIP code \
78205 is in San Antonio, Texas. These you can state with confidence.
  2. It is how forms of this kind are universally built. A form that asks for \
a lienholder only when the vehicle is financed. A benefits-eligibility flag \
that follows employment type.

Do NOT propose a rule for:

  - anything unique to a person: names, dates of birth, identifiers, email \
addresses, phone numbers, account numbers, free text
  - a free choice the applicant makes: marital status, payment plan, \
paperless billing, whether they want roadside assistance
  - a specific company's internal pricing or product ladder, which you cannot \
know. If a form clearly has one, say so in `why` and give a low confidence, \
so a person can correct it rather than trusting it.

Rules about fields that already have one will be ignored, so skip those.

For every rule, `why` must be a single plain sentence a reviewer can check \
without knowing anything about this system. It is the most important field \
you produce: a rule nobody can audit is worse than no rule at all. Set \
`confidence` to how sure you are that the real form enforces this, from 0 to \
1 - be honest, and use the low end. Every value you write must be one the \
target field can actually hold, copied exactly from its option list.
"""

RULES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "the field this rule decides",
                    },
                    "follows": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "the field or fields it follows",
                    },
                    "when": {
                        "type": "object",
                        "description": (
                            "source value to what this field may then hold. "
                            "With several sources, join their values with '|' "
                            "in the order `follows` gives. A single string "
                            "decides the field; a list of strings narrows it."
                        ),
                        "additionalProperties": True,
                    },
                    "otherwise": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "what it holds when `when` has no entry",
                    },
                    "confidence": {"type": "number"},
                    "why": {
                        "type": "string",
                        "description": "one plain sentence a reviewer can check",
                    },
                },
                "required": ["field", "follows", "when", "confidence", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rules"],
    "additionalProperties": False,
}


def rules_user(schema: FormSchema) -> str:
    """The turn that carries the form itself."""
    return (
        f"Form: {schema.name}\n"
        f"{len(schema.fields)} fields, one per line, as "
        f"name | label | meaning | control | [group] | [options]\n\n"
        f"{compact_schema(schema)}\n\n"
        "Propose the rules this form enforces."
    )


#: What one rule costs to write out, roughly, for the cost estimate. A rule
#: with a four-value table and a sentence of justification lands near here.
TOKENS_PER_RULE = 90
