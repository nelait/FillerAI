"""Rules, as opposed to statistics.

Some fields are not a guess at all. A full name is its parts joined up, a
middle initial is one letter of the middle name, an age is arithmetic on a
date of birth, and a "same as above" block is a copy. Counting co-occurrence
would eventually approximate these, badly and only for values it had already
seen; a rule gets them exactly right on a name that appears once.

Nothing here is trusted because it sounds plausible. Each rule is proposed
from the schema's semantic types, then **checked against the training data**,
and kept only if it actually held. That ordering is the whole point: the
semantic type says where to look, the data says whether it is true. A form
labelling something "Full Name" that turns out to hold an account handle
fails its check and is quietly dropped.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

from ..schema import FormSchema
from .features import Profile

# Below this a rule is a coincidence rather than a rule. Half of a form's
# records agreeing is not a derivation; it is two different habits.
MIN_ACCURACY = 0.9

# And it has to have been tested on enough records to mean anything.
MIN_SUPPORT = 5


@dataclass
class Derivation:
    """One verified way to compute a field from others."""

    kind: str
    target: str
    inputs: list[str]
    params: dict[str, Any] = dc_field(default_factory=dict)
    accuracy: float = 0.0
    support: int = 0

    def apply(self, observed: dict[str, str]) -> str | None:
        """Compute the target, or ``None`` if the inputs cannot support it."""
        values = [observed.get(name, "") for name in self.inputs]
        if any(not v for v in values):
            return None
        return _APPLY[self.kind](values, self.params)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "inputs": list(self.inputs),
            "params": dict(self.params),
            "accuracy": round(self.accuracy, 4),
            "support": self.support,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Derivation":
        return cls(
            kind=data["kind"],
            target=data["target"],
            inputs=list(data.get("inputs", [])),
            params=dict(data.get("params", {})),
            accuracy=float(data.get("accuracy", 0.0)),
            support=int(data.get("support", 0)),
        )


# ----------------------------------------------------------------------
# how each kind computes its value
# ----------------------------------------------------------------------


def _apply_copy(values: list[str], params: dict[str, Any]) -> str | None:
    return values[0]


def _apply_join(values: list[str], params: dict[str, Any]) -> str | None:
    template = params.get("template", "")
    try:
        return template.format(*values).strip()
    except (IndexError, KeyError):
        return None


def _apply_initial(values: list[str], params: dict[str, Any]) -> str | None:
    letter = values[0][0]
    letter = letter.upper() if params.get("upper", True) else letter
    return letter + str(params.get("suffix", ""))


def _apply_email(values: list[str], params: dict[str, Any]) -> str | None:
    template = params.get("template", "")
    domain = params.get("domain", "")
    if not domain:
        return None
    try:
        local = template.format(*[_ascii_local(v) for v in values])
    except (IndexError, KeyError):
        return None
    return f"{local}@{domain}" if local else None


def _apply_age(values: list[str], params: dict[str, Any]) -> str | None:
    born = parse_date(values[0])
    if born is None:
        return None
    today = parse_date(params.get("as_of", "")) or dt.date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return str(years) if years >= 0 else None


_APPLY: dict[str, Callable[[list[str], dict[str, Any]], str | None]] = {
    "copy": _apply_copy,
    "join": _apply_join,
    "initial": _apply_initial,
    "email": _apply_email,
    "age": _apply_age,
}


# ----------------------------------------------------------------------
# value helpers
# ----------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d.%m.%Y")
_NON_ASCII_LETTER = re.compile(r"[^a-z0-9]+")


def parse_date(text: str) -> dt.date | None:
    """Read a date in whichever of the common shapes the form used."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _ascii_local(text: str) -> str:
    """The form of a name that survives being put in an email local part."""
    folded = text.strip().lower()
    folded = folded.replace("ä", "a").replace("ö", "o").replace("ü", "u")
    folded = folded.replace("é", "e").replace("è", "e").replace("ñ", "n")
    return _NON_ASCII_LETTER.sub("", folded)


# ----------------------------------------------------------------------
# proposing candidates
# ----------------------------------------------------------------------

NAME_PART = {"first_name": 0, "middle_name": 1, "last_name": 2}

_JOIN_TEMPLATES = {
    ("first_name", "last_name"): ("{0} {1}", "{1}, {0}", "{1} {0}"),
    ("first_name", "middle_name", "last_name"): ("{0} {1} {2}", "{2}, {0} {1}"),
    ("first_name", "middle_name"): ("{0} {1}",),
}

_EMAIL_TEMPLATES = (
    "{0}.{1}", "{0}{1}", "{0}_{1}", "{0}-{1}",
    "{1}.{0}", "{1}{0}", "{0}",
)


def _by_group(schema: FormSchema) -> dict[str, dict[str, list[str]]]:
    """Field names indexed by group, then by semantic type.

    Grouping matters more than it looks: a rule must never join the
    applicant's first name to the spouse's surname. Phase 1 already sorted
    the fields into the groups that say who each one is about, so a candidate
    is only ever proposed inside one group.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for field in schema.fields:
        if field.constraints.read_only:
            continue
        out.setdefault(field.group or "", {}).setdefault(
            field.semantic_type, []).append(field.name)
    return out


def _candidates(schema: FormSchema, profiles: dict[str, Profile],
                columns: dict[str, list[str]]) -> list[Derivation]:
    candidates: list[Derivation] = []
    grouped = _by_group(schema)
    today = dt.date.today().isoformat()

    for group, by_type in grouped.items():
        first = (by_type.get("first_name") or [None])[0]
        middle = (by_type.get("middle_name") or [None])[0]
        last = (by_type.get("last_name") or [None])[0]

        for target in by_type.get("full_name", []):
            for parts, templates in _JOIN_TEMPLATES.items():
                names = [{"first_name": first, "middle_name": middle,
                          "last_name": last}[p] for p in parts]
                if any(n is None for n in names):
                    continue
                for template in templates:
                    candidates.append(Derivation(
                        kind="join", target=target, inputs=list(names),
                        params={"template": template}))

        if first and last:
            for target in by_type.get("email", []):
                domain = _modal_domain(columns.get(target, []))
                if not domain:
                    continue
                for template in _EMAIL_TEMPLATES:
                    candidates.append(Derivation(
                        kind="email", target=target, inputs=[first, last],
                        params={"template": template, "domain": domain}))

        for target in by_type.get("age", []):
            for source in by_type.get("date_of_birth", []):
                candidates.append(Derivation(
                    kind="age", target=target, inputs=[source],
                    params={"as_of": today}))

        # A one-character box next to a name is an initial. Proposing it for
        # every short field would be noise, so it is proposed only where the
        # form itself says the value is that short.
        for target, profile in profiles.items():
            field = schema.field(target)
            if field is None or (field.group or "") != group:
                continue
            if field.constraints.max_length not in (1, 2):
                continue
            for source in (middle, first, last):
                if not source or source == target:
                    continue
                for suffix in ("", "."):
                    candidates.append(Derivation(
                        kind="initial", target=target, inputs=[source],
                        params={"upper": True, "suffix": suffix}))

    # A copy is proposed everywhere, across groups included: "same as
    # billing" and a confirm-your-email box are both copies, and neither
    # respects a group boundary.
    names = [n for n, p in profiles.items() if p.filled > 0]
    for target in names:
        for source in names:
            if source != target:
                candidates.append(Derivation(kind="copy", target=target, inputs=[source]))
    return candidates


def _modal_domain(values: list[str]) -> str | None:
    """The domain most of the addresses share, when they share one.

    A derivation that has to guess the domain as well is not a derivation, so
    a column spread over many domains simply yields no candidate.
    """
    domains: dict[str, int] = {}
    total = 0
    for value in values:
        if "@" not in value:
            continue
        domain = value.rsplit("@", 1)[1].strip().lower()
        if not domain:
            continue
        domains[domain] = domains.get(domain, 0) + 1
        total += 1
    if not total:
        return None
    best = max(domains, key=lambda d: (domains[d], d))
    return best if domains[best] / total >= 0.5 else None


# ----------------------------------------------------------------------
# verification
# ----------------------------------------------------------------------


def _verify(candidate: Derivation, columns: dict[str, list[str]],
            rows: int) -> Derivation | None:
    target_values = columns.get(candidate.target)
    if target_values is None:
        return None
    input_values = [columns.get(name) for name in candidate.inputs]
    if any(v is None for v in input_values):
        return None

    hits = support = 0
    for index in range(rows):
        expected = target_values[index]
        if not expected:
            continue
        observed = {
            name: values[index] for name, values in zip(candidate.inputs, input_values)
        }
        produced = candidate.apply(observed)
        if produced is None:
            continue
        support += 1
        if produced == expected:
            hits += 1

    if support < MIN_SUPPORT:
        return None
    accuracy = hits / support
    if accuracy < MIN_ACCURACY:
        return None
    candidate.accuracy = accuracy
    candidate.support = support
    return candidate


# Which kind wins when two rules are equally accurate. A copy is the
# cheapest thing to believe and the easiest to read in a report, so it goes
# first; a join beats an initial, which is a lossy view of the same name.
_KIND_RANK = {"copy": 0, "age": 1, "join": 2, "email": 3, "initial": 4}


def learn_derivations(schema: FormSchema, records: list[dict[str, Any]],
                      profiles: dict[str, Profile],
                      columns: dict[str, list[str]]) -> dict[str, Derivation]:
    """Propose rules from the schema, keep the ones the data agrees with.

    One rule per field: the most accurate, then the best supported. A field
    with two competing explanations does not need both, and picking between
    them here keeps prediction a single lookup.
    """
    rows = len(records)
    best: dict[str, Derivation] = {}
    for candidate in _candidates(schema, profiles, columns):
        verified = _verify(candidate, columns, rows)
        if verified is None:
            continue
        incumbent = best.get(verified.target)
        key = (-verified.accuracy, -verified.support,
               _KIND_RANK.get(verified.kind, 9), verified.target)
        if incumbent is None:
            best[verified.target] = verified
            continue
        incumbent_key = (-incumbent.accuracy, -incumbent.support,
                         _KIND_RANK.get(incumbent.kind, 9), incumbent.target)
        if key < incumbent_key:
            best[verified.target] = verified

    # A rule whose input is itself produced by a rule is fine, but a cycle is
    # not: two fields each copying the other would leave prediction with no
    # place to start.
    return _break_cycles(best)


def _break_cycles(rules: dict[str, Derivation]) -> dict[str, Derivation]:
    """Keep every rule that does not close a loop back on itself.

    Two fields holding the same value verify as copies of each other, both
    ways. Keeping both would leave prediction with nowhere to start: each
    waits on the other. Keeping *neither* is the worse mistake, though - the
    relationship is real and one direction of it is perfectly usable - so
    rules are taken in a fixed order and one is dropped only when it would
    complete a cycle among the rules already kept.
    """
    kept: dict[str, Derivation] = {}

    def reaches(names: list[str], goal: str, seen: set[str]) -> bool:
        for name in names:
            if name == goal:
                return True
            rule = kept.get(name)
            if rule is None or name in seen:
                continue
            if reaches(rule.inputs, goal, seen | {name}):
                return True
        return False

    # Sorted, so which direction of a mutual pair survives is the same on
    # every run rather than whatever the dict happened to hold first.
    for target in sorted(rules):
        candidate = rules[target]
        if not reaches(candidate.inputs, target, {target}):
            kept[target] = candidate
    return kept
