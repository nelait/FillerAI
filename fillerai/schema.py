"""Field schema: the contract every other phase of FillerAI builds on.

A schema describes *what a form asks for*, independent of how the form was
found (scraped HTML, a hand-written field spec, an accessibility dump).
Extraction produces one; synthetic generation consumes one; the autofill
model and the simulator will both key off the same structure.

Stability rules for this format:

* ``SCHEMA_VERSION`` is bumped on any breaking change.
* Consumers must ignore unknown keys rather than fail, so new optional keys
  are additive and safe.
* ``Field.name`` is the stable join key. It is the form control's name where
  one exists, otherwise a slug derived from id/label.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Semantic types
# --------------------------------------------------------------------------
# The semantic type is what the field *means* ("this is a billing ZIP"), as
# opposed to its data type ("string"). Generation is driven entirely by the
# semantic type, so adding support for a new kind of field means adding a
# member here plus a renderer in fillerai.generate.render.

SEMANTIC_TYPES = (
    # identity
    "first_name",
    "middle_name",
    "last_name",
    "full_name",
    "prefix",
    "suffix",
    "date_of_birth",
    "age",
    "gender",
    "ssn",
    # contact
    "email",
    "phone",
    "phone_mobile",
    "url",
    # address
    "street_address",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    # organisation
    "company",
    "job_title",
    "department",
    "employee_id",
    # financial
    "credit_card_number",
    "credit_card_expiry",
    "credit_card_cvv",
    "account_number",
    "routing_number",
    "currency_amount",
    "iban",
    # insurance / claims, a common dense-form domain
    "policy_number",
    "claim_number",
    "group_number",
    "member_id",
    "diagnosis_code",
    # generic
    "date",
    "datetime",
    "time",
    "integer",
    "decimal",
    "percentage",
    "boolean",
    "enum",
    "free_text",
    "password",
    "unknown",
)

DATA_TYPES = ("string", "integer", "number", "boolean", "date", "datetime", "time")


@dataclass
class Constraints:
    """Machine-checkable limits read off the form control.

    Every value is optional: a constraint is recorded only when the source
    actually stated it, so ``None`` means "unconstrained", never "zero".
    """

    required: bool = False
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    multiple: bool = False  # control accepts more than one value
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        # Identity checks, not equality: ``0 == False`` in Python, so a
        # ``not in (None, False)`` test would silently drop ``minimum: 0``
        # and let a reloaded schema generate out-of-range numbers.
        return {
            k: v for k, v in asdict(self).items()
            if v is not None and v is not False
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Constraints":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Option:
    """One choice of a select / radio group / datalist."""

    value: str
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label or self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "Option":
        if isinstance(data, str):
            return cls(value=data, label=data)
        return cls(value=str(data.get("value", "")), label=data.get("label"))


@dataclass
class Field:
    """A single thing the form asks the agent to fill in."""

    name: str
    label: str | None = None
    semantic_type: str = "unknown"
    data_type: str = "string"
    control: str = "text"  # text, textarea, select, radio, checkbox, date, ...
    screen: str | None = None  # which screen/step the field lives on
    group: str | None = None  # coherence group, e.g. "billing" vs "shipping"
    options: list[Option] = dc_field(default_factory=list)
    constraints: Constraints = dc_field(default_factory=Constraints)
    placeholder: str | None = None
    help_text: str | None = None
    # How sure inference is about ``semantic_type``, 0.0-1.0, and why.
    confidence: float = 0.0
    evidence: list[str] = dc_field(default_factory=list)
    # Free-form escape hatch for source-specific detail (autocomplete tokens,
    # ARIA attributes, vendor metadata). Never load-bearing for generation.
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "semantic_type": self.semantic_type,
            "data_type": self.data_type,
            "control": self.control,
        }
        if self.screen:
            out["screen"] = self.screen
        if self.group:
            out["group"] = self.group
        if self.options:
            out["options"] = [o.to_dict() for o in self.options]
        constraints = self.constraints.to_dict()
        if constraints:
            out["constraints"] = constraints
        if self.placeholder:
            out["placeholder"] = self.placeholder
        if self.help_text:
            out["help_text"] = self.help_text
        out["confidence"] = round(self.confidence, 3)
        if self.evidence:
            out["evidence"] = self.evidence
        if self.extra:
            out["extra"] = self.extra
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Field":
        return cls(
            name=data["name"],
            label=data.get("label"),
            semantic_type=data.get("semantic_type", "unknown"),
            data_type=data.get("data_type", "string"),
            control=data.get("control", "text"),
            screen=data.get("screen"),
            group=data.get("group"),
            options=[Option.from_dict(o) for o in data.get("options", [])],
            constraints=Constraints.from_dict(data.get("constraints", {})),
            placeholder=data.get("placeholder"),
            help_text=data.get("help_text"),
            confidence=float(data.get("confidence", 0.0)),
            evidence=list(data.get("evidence", [])),
            extra=dict(data.get("extra", {})),
        )


@dataclass
class Screen:
    """One page or step of a multi-screen flow."""

    id: str
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Screen":
        return cls(id=data["id"], title=data.get("title"))


@dataclass
class FormSchema:
    """Everything known about one form, across all of its screens."""

    name: str
    fields: list[Field] = dc_field(default_factory=list)
    screens: list[Screen] = dc_field(default_factory=list)
    source: dict[str, Any] = dc_field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "source": self.source,
            "screens": [s.to_dict() for s in self.screens],
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FormSchema":
        version = data.get("schema_version", SCHEMA_VERSION)
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"schema_version {version!r} is not compatible with "
                f"{SCHEMA_VERSION!r} supported by this build of FillerAI"
            )
        return cls(
            name=data.get("name", "form"),
            fields=[Field.from_dict(f) for f in data.get("fields", [])],
            screens=[Screen.from_dict(s) for s in data.get("screens", [])],
            source=dict(data.get("source", {})),
            schema_version=version,
        )

    # -- convenience --------------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "FormSchema":
        return cls.from_dict(json.loads(text))

    def field(self, name: str) -> Field | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def fields_on(self, screen_id: str) -> list[Field]:
        return [f for f in self.fields if f.screen == screen_id]

    def groups(self) -> list[str]:
        """Distinct coherence groups, in first-seen order."""
        seen: list[str] = []
        for f in self.fields:
            g = f.group or ""
            if g not in seen:
                seen.append(g)
        return seen
