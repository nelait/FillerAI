"""Load a schema from a hand-written field specification.

Not every form can be handed over as HTML. A team that cannot share their
markup can still describe the fields, so this accepts a compact JSON spec and
produces the same :class:`~fillerai.schema.FormSchema` the HTML path does.

The compact form keeps a spec short enough to write by hand::

    {
      "name": "claims_intake",
      "screens": [{"id": "claimant", "title": "Claimant"}],
      "fields": [
        {"name": "policy_number", "label": "Policy Number",
         "screen": "claimant", "required": true, "max_length": 20},
        {"name": "billing_state", "label": "State", "group": "billing",
         "control": "select", "options": ["CA", "NY", "TX"]}
      ]
    }

Anything the spec states explicitly is taken as given; anything it leaves out
is inferred exactly as it would be from HTML.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import Constraints, Field, FormSchema, Option, Screen

# Keys that map straight onto Constraints, flattened for readability.
_CONSTRAINT_KEYS = {
    "required", "min_length", "max_length", "pattern",
    "minimum", "maximum", "step", "multiple", "read_only",
}


def _field_from_spec(entry: dict[str, Any]) -> Field:
    if "name" not in entry:
        raise ValueError(f"field spec is missing 'name': {entry!r}")

    constraints_block = dict(entry.get("constraints") or {})
    for key in _CONSTRAINT_KEYS:
        if key in entry:
            constraints_block[key] = entry[key]

    return Field(
        name=str(entry["name"]),
        label=entry.get("label"),
        # An explicit semantic type is honoured; otherwise inference decides.
        semantic_type=entry.get("semantic_type", "unknown"),
        data_type=entry.get("data_type", "string"),
        control=entry.get("control", entry.get("type", "text")),
        screen=entry.get("screen"),
        group=entry.get("group"),
        options=[Option.from_dict(o) for o in entry.get("options", [])],
        constraints=Constraints.from_dict(constraints_block),
        placeholder=entry.get("placeholder"),
        help_text=entry.get("help_text"),
        confidence=1.0 if entry.get("semantic_type") else 0.0,
        evidence=["declared in field spec"] if entry.get("semantic_type") else [],
        extra=dict(entry.get("extra") or {}),
    )


def load(data: dict[str, Any], source: dict | None = None) -> FormSchema:
    """Build a schema from a parsed spec document."""
    if "fields" not in data:
        raise ValueError("field spec must contain a 'fields' list")

    fields = [_field_from_spec(entry) for entry in data["fields"]]
    screens = [Screen.from_dict(s) if isinstance(s, dict) else Screen(id=str(s))
               for s in data.get("screens", [])]

    # Screens referenced by a field but never declared are still real screens.
    declared = {s.id for s in screens}
    for field in fields:
        if field.screen and field.screen not in declared:
            screens.append(Screen(id=field.screen))
            declared.add(field.screen)

    return FormSchema(
        name=data.get("name", "form"),
        fields=fields,
        screens=screens,
        source=source or {"kind": "spec"},
    )


def load_file(path: str | Path) -> FormSchema:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    # A previously extracted schema round-trips rather than being re-read as
    # a spec, so `extract` output can be edited and fed straight back in.
    if "schema_version" in data:
        return FormSchema.from_dict(data)
    return load(data, source={"kind": "spec", "path": str(path)})
