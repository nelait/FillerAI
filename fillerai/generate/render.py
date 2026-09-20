"""Render one field's value from a persona.

The persona already holds the facts; this module decides how a particular
control wants to receive them. That means picking the matching option out of
a ``<select>``, formatting a date the way the placeholder implies, and
trimming to ``maxlength`` without producing something the form would reject.
"""

from __future__ import annotations

import datetime as dt
import random
import re

from ..schema import Field, Option
from . import catalogs
from .persona import Persona

# Groups and name prefixes that mean "a different person", as opposed to
# "the same person's other address". A shipping address still belongs to the
# claimant; a referring provider does not.
PARTY_PREFIXES = frozenset({
    "spouse", "beneficiary", "emergency", "provider", "referring", "physician",
    "doctor", "coapplicant", "co_applicant", "cosigner", "guarantor", "witness",
    "dependent", "child", "parent", "guardian", "supervisor", "manager",
    "adjuster", "agent", "driver", "passenger", "operator", "contact_person",
    "next_of_kin", "landlord", "supervisor_contact",
})


def party_key(field: Field) -> str | None:
    """The other person this field is about, if it is about one at all."""
    group = (field.group or "").lower()
    if group in PARTY_PREFIXES:
        return group
    head = re.split(r"[._\-]", (field.name or "").lower())[0]
    if head in PARTY_PREFIXES:
        return head
    for prefix in PARTY_PREFIXES:
        if (field.name or "").lower().startswith(f"{prefix}_"):
            return prefix
    return None


# ----------------------------------------------------------------------
# option matching
# ----------------------------------------------------------------------


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def choose_option(field: Field, wanted: str | None, rng: random.Random) -> str | None:
    """Pick the option that matches ``wanted``, else any option at all.

    Returning an option's ``value`` (not its label) matters: that is what the
    form would submit.
    """
    if not field.options:
        return None
    if wanted:
        target = _normalise(wanted)
        for option in field.options:
            if _normalise(option.value) == target or _normalise(option.label or "") == target:
                return option.value
        # Fall back to a containment match so "California" finds "CA - California".
        for option in field.options:
            haystack = _normalise(f"{option.value} {option.label or ''}")
            if target and target in haystack:
                return option.value
    return rng.choice(field.options).value


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------

# Format hints read off a placeholder, checked before the control default.
_DATE_HINTS = (
    (re.compile(r"\bmm\s*/\s*dd\s*/\s*yyyy\b", re.I), "%m/%d/%Y"),
    (re.compile(r"\bdd\s*/\s*mm\s*/\s*yyyy\b", re.I), "%d/%m/%Y"),
    (re.compile(r"\byyyy\s*-\s*mm\s*-\s*dd\b", re.I), "%Y-%m-%d"),
    (re.compile(r"\bmm\s*-\s*dd\s*-\s*yyyy\b", re.I), "%m-%d-%Y"),
    (re.compile(r"\bmm\s*/\s*yy\b", re.I), "%m/%y"),
)


def _date_format(field: Field) -> str:
    if field.control in ("date", "datetime-local", "month", "time"):
        # A native date control always submits ISO, whatever it displays.
        return {"date": "%Y-%m-%d", "datetime-local": "%Y-%m-%dT%H:%M",
                "month": "%Y-%m", "time": "%H:%M"}[field.control]
    hint_source = f"{field.placeholder or ''} {field.help_text or ''} {field.extra.get('data-format', '')}"
    for pattern, fmt in _DATE_HINTS:
        if pattern.search(hint_source):
            return fmt
    return "%m/%d/%Y"


def _format_phone(field: Field, digits_source: str, rng: random.Random) -> str:
    """Shape a phone number to whatever the field appears to want."""
    digits = re.sub(r"\D", "", digits_source)
    area, exchange, line = digits[:3], digits[3:6], digits[6:10]
    hint = f"{field.placeholder or ''} {field.constraints.pattern or ''}"
    if re.search(r"\(\s*\d|\(\s*x", hint, re.I) or "(" in hint:
        return f"({area}) {exchange}-{line}"
    if re.search(r"\d{3}\.\d{3}\.\d{4}|x{3}\.x{3}", hint, re.I):
        return f"{area}.{exchange}.{line}"
    if field.constraints.max_length == 10 or re.fullmatch(r"\\?d\{10\}", hint.strip()):
        return digits
    if "+1" in hint or field.control == "tel" and "+" in hint:
        return f"+1{digits}"
    return f"{area}-{exchange}-{line}"


def _numeric_value(field: Field, rng: random.Random) -> float:
    """A number inside the declared bounds, snapped to ``step``."""
    constraints = field.constraints
    low = constraints.minimum if constraints.minimum is not None else 0.0
    high = constraints.maximum if constraints.maximum is not None else low + 1000.0
    if high < low:
        low, high = high, low
    value = rng.uniform(low, high)
    step = constraints.step
    if step and step > 0:
        value = low + round((value - low) / step) * step
        value = min(max(value, low), high)
    return value


def _format_number(field: Field, value: float) -> str:
    if field.data_type == "integer":
        return str(int(round(value)))
    return f"{value:.2f}"


def _fit_length(field: Field, text: str, rng: random.Random) -> str:
    """Keep a value inside ``maxlength``/``minlength``."""
    max_length = field.constraints.max_length
    if max_length is not None and len(text) > max_length:
        text = text[:max_length].rstrip()
    min_length = field.constraints.min_length
    if min_length is not None and len(text) < min_length:
        filler = " ".join(catalogs.LOREM_SENTENCES)
        text = (text + " " + filler)[:max(min_length, len(text))]
        if max_length is not None:
            text = text[:max_length]
    return text


def _free_text(field: Field, rng: random.Random) -> str:
    max_length = field.constraints.max_length
    budget = max_length if max_length is not None else (240 if field.control == "textarea" else 60)
    count = 3 if field.control == "textarea" else 1
    sentences = rng.sample(catalogs.LOREM_SENTENCES, min(count, len(catalogs.LOREM_SENTENCES)))
    return " ".join(sentences)[:budget].rstrip()


# ----------------------------------------------------------------------
# the renderer
# ----------------------------------------------------------------------


def render(field: Field, persona: Persona, rng: random.Random) -> object:
    """Produce the value this field would receive from ``persona``."""
    semantic = field.semantic_type
    group = field.group
    address = None

    # Fields about someone else on the form are rendered from that person.
    other = party_key(field)
    if other is not None:
        persona = persona.related(other)

    # --- identity -----------------------------------------------------
    if semantic == "first_name":
        value = persona.first_name
    elif semantic == "middle_name":
        # A one-character box is asking for the initial, not the name.
        if field.constraints.max_length == 1 or "initial" in (field.label or "").lower():
            value = persona.middle_name[:1]
        else:
            value = persona.middle_name
    elif semantic == "last_name":
        value = persona.last_name
    elif semantic == "full_name":
        value = persona.full_name
    elif semantic == "prefix":
        return _render_prefix(field, persona, rng)
    elif semantic == "suffix":
        value = persona.suffix
    elif semantic == "gender":
        value = persona.gender
    elif semantic == "age":
        value = str(persona.age)
    elif semantic == "date_of_birth":
        value = persona.date_of_birth.strftime(_date_format(field))
    elif semantic == "ssn":
        raw = persona.identifier("ssn")
        value = re.sub(r"\D", "", raw) if field.constraints.max_length == 9 else raw

    # --- contact ------------------------------------------------------
    elif semantic == "email":
        value = persona.email()
    elif semantic in ("phone", "phone_mobile"):
        value = _format_phone(field, persona.phone(semantic, group), rng)
    elif semantic == "url":
        company = persona.employment().company
        slug = re.sub(r"[^a-z0-9]", "", company.split()[0].lower())
        value = f"https://www.{slug}.example.com"

    # --- address ------------------------------------------------------
    elif semantic == "street_address":
        address = persona.address(group)
        value = address.street
    elif semantic == "address_line2":
        address = persona.address(group)
        value = address.line2 or ""
    elif semantic == "city":
        value = persona.address(group).city
    elif semantic == "state":
        address = persona.address(group)
        # Some forms want the code, others the full name; the option list
        # decides when there is one, otherwise the field's width does.
        if field.options:
            return choose_option(field, address.state, rng) or address.state
        wants_name = (field.constraints.max_length or 99) > 2
        value = address.state_name if wants_name else address.state
    elif semantic == "postal_code":
        address = persona.address(group)
        value = address.postal_code
        if (field.constraints.max_length or 0) >= 10 and rng.random() < 0.3:
            value = f"{value}-{rng.randint(1000, 9999)}"
    elif semantic == "country":
        if field.options:
            return choose_option(field, "United States", rng) or "US"
        wants_name = (field.constraints.max_length or 99) > 2
        value = "United States" if wants_name else "US"

    # --- organisation --------------------------------------------------
    elif semantic == "company":
        value = persona.employment().company
    elif semantic == "job_title":
        value = persona.employment().job_title
    elif semantic == "department":
        value = persona.employment().department
    elif semantic == "employee_id":
        value = persona.employment().employee_id

    # --- financial ------------------------------------------------------
    elif semantic == "credit_card_number":
        number = persona.card("number")
        value = number if not field.constraints.pattern else re.sub(r"\D", "", number)
    elif semantic == "credit_card_expiry":
        value = persona.card("expiry")
    elif semantic == "credit_card_cvv":
        value = persona.card("cvv")
    elif semantic in ("account_number", "routing_number", "iban",
                      "policy_number", "claim_number", "group_number", "member_id"):
        value = persona.identifier(semantic)
    elif semantic == "currency_amount":
        bounded = (field.constraints.minimum is not None
                   or field.constraints.maximum is not None)
        amount = _numeric_value(field, rng) if bounded else round(rng.uniform(25, 9500), 2)
        value = f"{amount:.2f}"
    elif semantic == "percentage":
        constraints = field.constraints
        low = constraints.minimum if constraints.minimum is not None else 0
        high = constraints.maximum if constraints.maximum is not None else 100
        value = _format_number(field, rng.uniform(low, high))
    elif semantic == "diagnosis_code":
        code, description = rng.choice(catalogs.DIAGNOSIS_CODES)
        value = code if (field.constraints.max_length or 99) < 20 else f"{code} - {description}"

    # --- generic ---------------------------------------------------------
    elif semantic in ("date", "datetime", "time"):
        return _render_date_like(field, persona, rng)
    elif semantic == "boolean":
        # ``required`` on a checkbox means the form will not submit unless it
        # is ticked, so a consent box is always true rather than a coin flip.
        flag = True if (field.control == "checkbox" and field.constraints.required) \
            else rng.random() < 0.5
        if field.options:
            return choose_option(field, "Yes" if flag else "No", rng)
        if field.control == "checkbox":
            return flag
        return "Yes" if flag else "No"
    elif semantic in ("integer", "decimal"):
        return _format_number(field, _numeric_value(field, rng))
    elif semantic == "password":
        value = _password(rng, field)
    elif semantic == "enum":
        return choose_option(field, None, rng) or ""
    else:  # free_text and unknown
        if field.options:
            return choose_option(field, None, rng) or ""
        if field.control == "number":
            return _format_number(field, _numeric_value(field, rng))
        value = _free_text(field, rng)

    if field.options:
        # Any field backed by a closed set must submit one of its own values.
        matched = choose_option(field, str(value), rng)
        if matched is not None:
            return matched
    return _fit_length(field, str(value), rng)


def compatible_prefixes(field: Field, gender: str) -> list[str]:
    """Titles this field can submit that also agree with ``gender``."""
    allowed = catalogs.PREFIXES_BY_GENDER.get(gender, catalogs.NAME_PREFIXES)
    if not field.options:
        return list(allowed)
    return [
        option.value for option in field.options
        if option.value in allowed or (option.label or "") in allowed
    ]


def _render_prefix(field: Field, persona: Persona, rng: random.Random) -> str:
    """An honorific that agrees with the persona and the form's own list.

    Falling back to a random option here is what produced "Mr." beside
    "Non-binary": the persona's own title was not on the list, so any option
    would do. A title is better left blank than made to contradict the record.
    """
    compatible = compatible_prefixes(field, persona.gender)
    if persona.prefix and persona.prefix in compatible:
        return persona.prefix
    if compatible:
        return rng.choice(compatible)
    # The form offers no title this persona could wear.
    if not field.constraints.required:
        return ""
    return choose_option(field, None, rng) or ""


def _render_date_like(field: Field, persona: Persona, rng: random.Random) -> str:
    """A plain date, placed on the side of today its label implies."""
    label = f"{field.name} {field.label or ''}".lower()
    today = persona.today
    future_words = ("expir", "due", "renew", "next", "appointment", "schedul", "effective_end", "end")
    past_words = ("birth", "hire", "start", "incident", "accident", "loss",
                  "issue", "since", "last", "effective", "filed", "reported")

    lower_bound = field.extra.get("date_min")
    upper_bound = field.extra.get("date_max")
    if any(word in label for word in future_words):
        start, end = today, today + dt.timedelta(days=540)
    elif any(word in label for word in past_words):
        start, end = today - dt.timedelta(days=1460), today
    else:
        start, end = today - dt.timedelta(days=365), today + dt.timedelta(days=365)

    start = _parse_iso(lower_bound) or start
    end = _parse_iso(upper_bound) or end
    if end < start:
        start, end = end, start

    chosen = start + dt.timedelta(days=rng.randint(0, max((end - start).days, 0)))
    if field.semantic_type == "datetime" or field.control == "datetime-local":
        moment = dt.datetime.combine(chosen, dt.time(rng.randint(8, 18), rng.choice([0, 15, 30, 45])))
        return moment.strftime(_date_format(field))
    if field.semantic_type == "time" or field.control == "time":
        return f"{rng.randint(8, 18):02d}:{rng.choice([0, 15, 30, 45]):02d}"
    return chosen.strftime(_date_format(field))


def _parse_iso(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _password(rng: random.Random, field: Field) -> str:
    length = max(field.constraints.min_length or 12, 12)
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%"
    return "".join(rng.choice(alphabet) for _ in range(length))
