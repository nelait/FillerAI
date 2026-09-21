"""Turn a schema into a dataset of coherent records."""

from __future__ import annotations

import csv
import io
import json
import random
import re
from dataclasses import dataclass, field as dc_field

from ..schema import Field, FormSchema
from .persona import Persona
from .render import choose_option, render


@dataclass
class Options:
    """Knobs for a generation run."""

    count: int = 10
    seed: int | None = None
    # Share of optional fields left empty, so downstream training sees the
    # partially-filled forms it will meet in production. 0 fills everything.
    blank_rate: float = 0.12
    safe_identifiers: bool = True
    # Include the persona behind each record, handy for debugging coherence.
    include_persona: bool = False


@dataclass
class Dataset:
    schema: FormSchema
    records: list[dict[str, object]] = dc_field(default_factory=list)

    # -- serialisation ------------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {"form": self.schema.name, "count": len(self.records), "records": self.records},
            indent=indent, ensure_ascii=False, default=str,
        )

    def to_ndjson(self) -> str:
        return "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in self.records)

    def to_csv(self) -> str:
        columns = [f.name for f in self.schema.fields]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in self.records:
            writer.writerow({k: _csv_cell(record.get(k)) for k in columns})
        return buffer.getvalue()

    def render(self, fmt: str) -> str:
        if fmt == "json":
            return self.to_json()
        if fmt == "ndjson":
            return self.to_ndjson()
        if fmt == "csv":
            return self.to_csv()
        raise ValueError(f"unknown output format {fmt!r}; expected json, ndjson or csv")


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    return str(value)


# ----------------------------------------------------------------------
# generation
# ----------------------------------------------------------------------


ADDRESS_SEMANTICS = {"street_address", "city", "state", "postal_code", "address_line2"}


def _primary_address_group(schema: FormSchema) -> str | None:
    """The group holding the person's own address.

    Preference goes to a group whose name says it is where the person lives,
    then to the first group carrying address fields at all.
    """
    groups_with_address: list[str] = []
    for field in schema.fields:
        if field.semantic_type in ADDRESS_SEMANTICS:
            group = field.group or ""
            if group not in groups_with_address:
                groups_with_address.append(group)
    if not groups_with_address:
        return None
    for preferred in ("", "home", "primary", "applicant", "insured", "patient", "billing"):
        if preferred in groups_with_address:
            return preferred
    return groups_with_address[0]


def _prepare(schema: FormSchema, persona: Persona) -> None:
    """Apply every schema-wide constraint before any value is rendered.

    Order matters: the persona has to know which states the form offers, and
    which address is the person's own, before it creates either.
    """
    persona.set_primary_address_group(_primary_address_group(schema))
    for field in schema.fields:
        if field.semantic_type != "state" or not field.options:
            continue
        codes = tuple(
            o.value.strip().upper()
            for o in field.options
            if re.fullmatch(r"[A-Za-z]{2}", o.value.strip())
        )
        if codes:
            persona.constrain_states(field.group, codes)


def _blank_address_groups(schema: FormSchema, options: Options,
                          rng: random.Random) -> set[str]:
    """Decide which optional address blocks are omitted wholesale.

    An optional address is present or absent as a unit. Blanking its fields
    one by one would leave a city with no state, which is exactly the kind of
    half-filled record a model must not learn from.
    """
    if options.blank_rate <= 0:
        return set()
    blanked: set[str] = set()
    by_group: dict[str, list[Field]] = {}
    for field in schema.fields:
        if field.semantic_type in ADDRESS_SEMANTICS:
            by_group.setdefault(field.group or "", []).append(field)
    for group, fields in by_group.items():
        if any(f.constraints.required for f in fields):
            continue
        # An optional address block is left out more often than a lone
        # optional field, matching how "if different" sections behave.
        if rng.random() < max(options.blank_rate, 0.35):
            blanked.add(group)
    return blanked


def _should_blank(field: Field, options: Options, rng: random.Random,
                  blanked_groups: set[str]) -> bool:
    if field.constraints.required or options.blank_rate <= 0:
        return False
    if field.semantic_type in ADDRESS_SEMANTICS:
        if (field.group or "") in blanked_groups:
            return True
        # A second address line is genuinely optional within a present block.
        return field.semantic_type == "address_line2" and rng.random() < 0.5
    # Fields that are usually empty on real forms stay empty more often.
    rate = options.blank_rate
    if field.semantic_type in ("address_line2", "middle_name", "suffix", "prefix"):
        rate = max(rate, 0.5)
    return rng.random() < rate


# ----------------------------------------------------------------------
# declared rules
# ----------------------------------------------------------------------


def _resolution_order(schema: FormSchema) -> list[Field]:
    """Fields ordered so a rule's sources are rendered before it.

    Declared rules form a dependency graph, and the schema's own order says
    nothing about it - a form may well ask for the deductible above the plan
    tier that decides it. A depth-first walk puts every source first.

    A rule naming a field that is not on the form, or a cycle of fields that
    follow each other, leaves the fields involved in their original places.
    Neither can be resolved, and the generator's job is to produce a record
    anyway: those fields fall back to being rendered normally, and
    :func:`coherence_report` is what says the rule did not hold.
    """
    by_name = {f.name: f for f in schema.fields}
    order: list[Field] = []
    placed: set[str] = set()
    walking: set[str] = set()

    def visit(field: Field) -> None:
        if field.name in placed or field.name in walking:
            return  # already done, or a cycle - either way, stop here
        walking.add(field.name)
        if field.derived:
            for source in field.derived.sources:
                parent = by_name.get(source)
                if parent is not None:
                    visit(parent)
        walking.discard(field.name)
        placed.add(field.name)
        order.append(field)

    for field in schema.fields:
        visit(field)
    return order


def _by_rule(field: Field, so_far: dict[str, object],
             rng: random.Random) -> object | None:
    """This field's value under its declared rule, or None if it does not apply.

    A rule needs its sources answered. On a form where the source is blank -
    an optional section nobody filled in - there is nothing to follow, and
    the field is rendered the ordinary way instead.
    """
    rule = field.derived
    if rule is None:
        return None
    values: list[str] = []
    for source in rule.sources:
        seen = so_far.get(source)
        if seen is None or str(seen) == "":
            return None
        values.append(str(seen))

    allowed = rule.lookup(values)
    if not allowed:
        return None
    return _as_written(field, rng.choice(list(allowed)))


def _as_written(field: Field, wanted: str) -> str:
    """What a rule's value is called on this field.

    A rule is written in the business's words and the field submits option
    values, which are not always the same string. On a field with options
    the rule's value is resolved to the option it names; where it names none
    it is handed back untouched, so that :func:`validate` reports it as not
    one of the field's options. Deliberately not ``choose_option``, whose
    fallback is a *random* option: that would turn a rule the spec got wrong
    into noise indistinguishable from the noise this whole feature exists to
    remove.
    """
    if not field.options:
        return wanted
    target = _normalise_value(wanted)
    for option in field.options:
        if _normalise_value(option.value) == target:
            return option.value
    for option in field.options:
        if target and target in _normalise_value(f"{option.value} {option.label or ''}"):
            return option.value
    return wanted


def _normalise_value(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def generate(schema: FormSchema, options: Options | None = None) -> Dataset:
    """Generate ``options.count`` coherent records for ``schema``."""
    options = options or Options()
    dataset = Dataset(schema=schema)
    base_seed = options.seed

    for index in range(options.count):
        # Seeding per record keeps any single record reproducible on its own,
        # which makes a failing case easy to isolate.
        seed = None if base_seed is None else base_seed + index
        rng = random.Random(seed)
        persona = Persona(rng=rng, safe_identifiers=options.safe_identifiers)
        _prepare(schema, persona)

        blanked_groups = _blank_address_groups(schema, options, rng)

        # Rendered in dependency order so a rule can read what it follows,
        # then emitted in the form's own order: the record is the form, and
        # the order the generator happened to need is nobody else's business.
        filled: dict[str, object] = {}
        for field in _resolution_order(schema):
            if field.constraints.read_only:
                continue
            if _should_blank(field, options, rng, blanked_groups):
                filled[field.name] = ""
                continue
            ruled = _by_rule(field, filled, rng)
            filled[field.name] = (ruled if ruled is not None
                                  else render(field, persona, rng))
        record: dict[str, object] = {
            f.name: filled[f.name] for f in schema.fields if f.name in filled
        }

        if options.include_persona:
            record["_persona"] = {
                "name": persona.full_name,
                "date_of_birth": persona.date_of_birth.isoformat(),
                "addresses": {
                    key or "default": f"{a.street}, {a.city}, {a.state} {a.postal_code}"
                    for key, a in persona._addresses.items()
                },
            }
        dataset.records.append(record)

    return dataset


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------


def validate(schema: FormSchema, records: list[dict[str, object]]) -> list[str]:
    """Check records against the schema's own constraints.

    Returns a list of human-readable problems, empty when the dataset would
    be accepted by the form it came from. This is the guard that keeps a
    generator change from quietly producing values the target form rejects.
    """
    problems: list[str] = []
    for index, record in enumerate(records):
        for field in schema.fields:
            if field.constraints.read_only:
                continue
            if field.name not in record:
                problems.append(f"record {index}: missing field {field.name!r}")
                continue
            value = record[field.name]

            # A boolean is always present, including when it is False, so it
            # is checked for truthiness where required and skipped otherwise.
            if isinstance(value, bool):
                if field.constraints.required and not value:
                    problems.append(
                        f"record {index}: required checkbox {field.name!r} is not ticked"
                    )
                continue

            text = "" if value is None else str(value)
            if field.constraints.required and text == "":
                problems.append(f"record {index}: required field {field.name!r} is empty")
                continue
            if text == "":
                continue

            constraints = field.constraints
            if constraints.max_length is not None and len(text) > constraints.max_length:
                problems.append(
                    f"record {index}: {field.name!r} is {len(text)} chars, "
                    f"over maxlength {constraints.max_length}"
                )
            if constraints.min_length is not None and len(text) < constraints.min_length:
                problems.append(
                    f"record {index}: {field.name!r} is {len(text)} chars, "
                    f"under minlength {constraints.min_length}"
                )
            if constraints.pattern:
                try:
                    if not re.fullmatch(constraints.pattern, text):
                        problems.append(
                            f"record {index}: {field.name!r} value {text!r} "
                            f"fails pattern {constraints.pattern!r}"
                        )
                except re.error:
                    pass  # A pattern Python cannot compile is not the data's fault.
            if field.options and not constraints.multiple:
                allowed = {o.value for o in field.options}
                if text not in allowed:
                    problems.append(
                        f"record {index}: {field.name!r} value {text!r} is not one of its options"
                    )
            if field.data_type in ("integer", "number"):
                try:
                    number = float(text)
                except ValueError:
                    problems.append(f"record {index}: {field.name!r} value {text!r} is not numeric")
                    continue
                if constraints.minimum is not None and number < constraints.minimum:
                    problems.append(
                        f"record {index}: {field.name!r} value {number} below min {constraints.minimum}"
                    )
                if constraints.maximum is not None and number > constraints.maximum:
                    problems.append(
                        f"record {index}: {field.name!r} value {number} above max {constraints.maximum}"
                    )
    return problems


def coherence_report(schema: FormSchema, records: list[dict[str, object]]) -> list[str]:
    """Check the cross-field relationships the generator promises.

    Validation above asks "would the form accept this?"; this asks the harder
    question, "does the record describe one consistent entity?".
    """
    from . import catalogs
    from .render import compatible_prefixes

    problems: list[str] = []
    by_group: dict[str, dict[str, str]] = {}
    for field in schema.fields:
        by_group.setdefault(field.group or "", {})[field.semantic_type] = field.name

    for index, record in enumerate(records):
        for group, fields in by_group.items():
            prefix_key, gender_key = fields.get("prefix"), fields.get("gender")
            if prefix_key and gender_key:
                prefix = str(record.get(prefix_key, ""))
                gender = str(record.get(gender_key, ""))
                allowed = catalogs.PREFIXES_BY_GENDER.get(gender)
                prefix_field = schema.field(prefix_key)
                # Only a contradiction the form left room to avoid counts.
                avoidable = prefix_field is not None and compatible_prefixes(
                    prefix_field, gender
                )
                if prefix and allowed and avoidable and prefix not in allowed:
                    problems.append(
                        f"record {index} [{group or 'default'}]: title {prefix!r} "
                        f"does not agree with gender {gender!r}"
                    )

            city_key, state_key, zip_key = (
                fields.get("city"), fields.get("state"), fields.get("postal_code")
            )
            city = str(record.get(city_key, "")) if city_key else ""
            state = str(record.get(state_key, "")) if state_key else ""
            postal = str(record.get(zip_key, ""))[:5] if zip_key else ""

            # An address is all there or not there at all; a city with no
            # state is the incoherent middle ground.
            present = {
                label: value
                for label, key, value in (
                    ("city", city_key, city), ("state", state_key, state),
                    ("postal_code", zip_key, postal),
                )
                if key is not None
            }
            filled = [k for k, v in present.items() if v]
            if present and filled and len(filled) != len(present):
                missing = sorted(set(present) - set(filled))
                problems.append(
                    f"record {index} [{group or 'default'}]: address is partly "
                    f"filled, missing {', '.join(missing)}"
                )
                continue
            if not city or not state:
                continue
            matches = [
                p for p in catalogs.PLACES
                if p.city == city and (p.state == state or p.state_name == state)
            ]
            if not matches:
                problems.append(
                    f"record {index} [{group or 'default'}]: city {city!r} "
                    f"does not belong to state {state!r}"
                )
            elif postal and not any(postal in p.zip_codes for p in matches):
                problems.append(
                    f"record {index} [{group or 'default'}]: ZIP {postal!r} "
                    f"does not belong to {city}, {state}"
                )

    problems.extend(_rule_problems(schema, records))
    return problems


def _rule_problems(schema: FormSchema, records: list[dict[str, object]]) -> list[str]:
    """Check that every rule the form declared actually held.

    A rule the generator quietly failed to apply is worse than no rule: the
    dataset looks structured and is not, and the model trained on it finds
    nothing while appearing to have had its chance. So the rules are checked
    against the records the same way the addresses are.

    Reported once per field rather than once per record. A rule that is wrong
    is wrong in every record, and a thousand copies of that would bury the
    address problems underneath it.
    """
    problems: list[str] = []
    known = {f.name for f in schema.fields}
    for field in schema.fields:
        rule = field.derived
        if rule is None:
            continue
        missing = [s for s in rule.sources if s not in known]
        if missing:
            problems.append(
                f"{field.name!r} follows {', '.join(repr(m) for m in missing)}, "
                f"which this form does not ask for"
            )
            continue
        broken = 0
        for record in records:
            values = [str(record.get(s, "")) for s in rule.sources]
            if any(v == "" for v in values):
                continue  # nothing to follow; the field was rendered normally
            allowed = rule.lookup(values)
            if not allowed:
                continue  # the table does not cover this combination
            seen = str(record.get(field.name, ""))
            if seen == "":
                continue  # blanked, which the rule does not forbid
            if seen not in {_as_written(field, a) for a in allowed}:
                broken += 1
        if broken:
            problems.append(
                f"{field.name!r} does not follow {'/'.join(rule.sources)} "
                f"in {broken} of {len(records)} records"
            )
    return problems
