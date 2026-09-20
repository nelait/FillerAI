"""Reading a dataset the way the model needs to see it.

Everything in here is about turning the loose values a form produces -
strings, booleans, missing keys, multi-selects - into something countable,
and then describing each column well enough that the model knows what kind
of prediction is even possible for it.

The distinction that matters is between a field whose values form a closed
set the model can choose from, and one whose values are essentially unique
per record. A state dropdown is the first; a claim number is the second. No
amount of training data makes the second predictable, and a model that
pretends otherwise is worse than one that says "you will have to type this".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from ..schema import Field, FormSchema

# A field is treated as enumerable when its values form a set small enough
# to choose from. Two limits rather than one: an absolute ceiling so a model
# never carries thousands of classes, and a share-of-rows test so twenty
# distinct values across twenty records is recognised as "all different"
# rather than "a twenty-way choice".
MAX_DISTINCT = 500
MIN_ROWS_FOR_SHARE = 32

# Below this many filled rows a field has not shown enough of itself to
# classify honestly, so it is left alone rather than guessed at.
MIN_ROWS = 3


def normalise(value: Any) -> str:
    """Collapse one cell to the string the model counts.

    Booleans become words rather than ``True``/``False`` so a model loaded
    from JSON behaves the same as one just fitted, and a multi-select keeps
    the ``|`` joining that :mod:`fillerai.generate.dataset` already uses for
    CSV, so the same record reads identically whichever way it arrived.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "|".join(normalise(v) for v in value)
    return str(value).strip()


def column(records: Iterable[dict[str, Any]], name: str) -> list[str]:
    """Every value of one field, normalised, blanks included."""
    return [normalise(r.get(name)) for r in records]


# ----------------------------------------------------------------------
# format masks
# ----------------------------------------------------------------------

_MASK_MAP = {"digit": "9", "upper": "A", "lower": "a"}


def mask_of(text: str) -> str:
    """A shape for one value: ``CLM-004821`` becomes ``AAA-999999``.

    Only used on fields the model cannot predict, where the shape is the one
    useful thing left to say about them.
    """
    out = []
    for char in text:
        if char.isdigit():
            out.append(_MASK_MAP["digit"])
        elif char.isupper():
            out.append(_MASK_MAP["upper"])
        elif char.islower():
            out.append(_MASK_MAP["lower"])
        else:
            out.append(char)
    return "".join(out)


def dominant_mask(values: Iterable[str], threshold: float = 0.9) -> str | None:
    """The one shape most values share, or ``None`` if they do not share one."""
    filled = [v for v in values if v]
    if len(filled) < MIN_ROWS:
        return None
    counts = Counter(mask_of(v) for v in filled)
    mask, hits = counts.most_common(1)[0]
    return mask if hits / len(filled) >= threshold else None


# ----------------------------------------------------------------------
# per-field profile
# ----------------------------------------------------------------------


@dataclass
class Profile:
    """What one column of the training data turned out to look like."""

    name: str
    semantic_type: str = "unknown"
    group: str | None = None
    screen: str | None = None
    rows: int = 0  # records seen
    filled: int = 0  # records where the field had a value
    distinct: int = 0
    kind: str = "open"  # "constant" | "enumerable" | "open"
    values: list[tuple[str, int]] = dc_field(default_factory=list)  # value, count
    format: str | None = None  # shape, for open fields only
    closed: bool = False  # the form itself limits this to a list of options

    # -- derived views -----------------------------------------------------

    @property
    def fill_rate(self) -> float:
        return self.filled / self.rows if self.rows else 0.0

    @property
    def total(self) -> int:
        return sum(count for _, count in self.values)

    def distribution(self) -> dict[str, float]:
        """How the field's values are spread, over filled rows only.

        Blanks are left out on purpose. A blank on a form means "not
        applicable here", which is a question about whether to fill the field
        at all - a different question from what to fill it with, and not one
        autofill is being asked.
        """
        total = self.total
        if not total:
            return {}
        return {value: count / total for value, count in self.values}

    def modal(self) -> tuple[str, float] | None:
        dist = self.distribution()
        if not dist:
            return None
        value = max(dist, key=lambda v: dist[v])
        return value, dist[value]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "semantic_type": self.semantic_type,
            "rows": self.rows,
            "filled": self.filled,
            "distinct": self.distinct,
            "kind": self.kind,
        }
        if self.group:
            out["group"] = self.group
        if self.screen:
            out["screen"] = self.screen
        if self.values:
            out["values"] = [[v, c] for v, c in self.values]
        if self.format:
            out["format"] = self.format
        if self.closed:
            out["closed"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            name=data["name"],
            semantic_type=data.get("semantic_type", "unknown"),
            group=data.get("group"),
            screen=data.get("screen"),
            rows=int(data.get("rows", 0)),
            filled=int(data.get("filled", 0)),
            distinct=int(data.get("distinct", 0)),
            kind=data.get("kind", "open"),
            values=[(str(v), int(c)) for v, c in data.get("values", [])],
            format=data.get("format"),
            closed=bool(data.get("closed", False)),
        )


def profile_field(field: Field, values: list[str], keep_values: int = MAX_DISTINCT) -> Profile:
    """Describe one column: how full it is, and whether it can be chosen from."""
    filled = [v for v in values if v]
    counts = Counter(filled)
    distinct = len(counts)

    # A ``<select>`` states its own closed set, which beats anything counted
    # off a sample: the options are the form's rule, not the data's habit.
    closed = bool(field.options) and not field.constraints.multiple

    if len(filled) < MIN_ROWS:
        kind = "enumerable" if closed else "open"
    elif distinct == 1:
        kind = "constant"
    elif closed:
        kind = "enumerable"
    elif distinct <= MAX_DISTINCT and distinct <= max(MIN_ROWS_FOR_SHARE, len(filled) * 0.5):
        kind = "enumerable"
    else:
        kind = "open"

    profile = Profile(
        name=field.name,
        semantic_type=field.semantic_type,
        group=field.group,
        screen=field.screen,
        rows=len(values),
        filled=len(filled),
        distinct=distinct,
        kind=kind,
        closed=closed,
    )
    if kind in ("constant", "enumerable"):
        profile.values = counts.most_common(keep_values)
    else:
        # Keeping a handful of examples is still worth it: the UI shows them
        # so a person can see what the field wants.
        profile.values = counts.most_common(5)
        profile.format = dominant_mask(filled)
    return profile


def profile_all(schema: FormSchema, records: list[dict[str, Any]]) -> dict[str, Profile]:
    """Profile every writable field of the schema against the dataset."""
    out: dict[str, Profile] = {}
    for field in schema.fields:
        if field.constraints.read_only:
            continue
        out[field.name] = profile_field(field, column(records, field.name))
    return out


# ----------------------------------------------------------------------
# small shared helpers
# ----------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall(text)
