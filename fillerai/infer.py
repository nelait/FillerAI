"""Decide what each extracted field *means*.

Extraction records what the markup says; inference decides that
``<input name="bill_zip">`` is a postal code. Signals are ranked by how much
they can be trusted, and every decision carries the evidence that produced it
so a low-confidence guess can be reviewed rather than silently believed.
"""

from __future__ import annotations

import re

from .schema import Field, FormSchema

# Confidence attached to each class of signal. ``autocomplete`` is a spec-
# defined token authored deliberately, so it outranks a name that merely
# happens to contain a keyword.
WEIGHT_AUTOCOMPLETE = 0.97
WEIGHT_INPUT_TYPE = 0.90
WEIGHT_NAME = 0.80
WEIGHT_LABEL = 0.74
WEIGHT_OPTIONS = 0.72
WEIGHT_WEAK = 0.55  # placeholder, inputmode, pattern

# WHATWG autocomplete tokens -> semantic type.
AUTOCOMPLETE_MAP = {
    "name": "full_name",
    "given-name": "first_name",
    "additional-name": "middle_name",
    "family-name": "last_name",
    "honorific-prefix": "prefix",
    "honorific-suffix": "suffix",
    "nickname": "free_text",
    "organization": "company",
    "organization-title": "job_title",
    "street-address": "street_address",
    "address-line1": "street_address",
    "address-line2": "address_line2",
    "address-level1": "state",
    "address-level2": "city",
    "postal-code": "postal_code",
    "country": "country",
    "country-name": "country",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "url": "url",
    "bday": "date_of_birth",
    "sex": "gender",
    "cc-number": "credit_card_number",
    "cc-exp": "credit_card_expiry",
    "cc-csc": "credit_card_cvv",
    "cc-name": "full_name",
    "current-password": "password",
    "new-password": "password",
}

# HTML input types that name their own meaning.
INPUT_TYPE_MAP = {
    "email": "email",
    "tel": "phone",
    "url": "url",
    "password": "password",
    "date": "date",
    "datetime-local": "datetime",
    "time": "time",
    "month": "date",
    "number": "decimal",
    "range": "integer",
    "checkbox": "boolean",
    "color": "free_text",
}

# Ordered most-specific first: the first rule that matches wins, so
# "date of birth" is settled before the generic "date" rule is reached and
# "company name" never falls through to "name".
#
# Each entry is (semantic_type, name_pattern, label_pattern). A pattern of
# None means that signal is not used for the type.
RULES: list[tuple[str, str | None, str | None]] = [
    # identity, specific before generic
    ("date_of_birth", r"(^|[._\-])(dob|birth_?date|date_?of_?birth|bday|birthday)([._\-]|$)",
     r"\b(date of birth|birth ?date|d\.?o\.?b\.?|birthday)\b"),
    ("ssn", r"(^|[._\-])(ssn|social_?security|tax_?id|tin)([._\-]|$)",
     r"\b(ssn|social security|tax id|tin)\b"),
    ("first_name", r"(^|[._\-])(f_?name|first_?name|given_?name|forename)([._\-]|$)",
     r"\b(first|given|fore)\s*name\b"),
    ("middle_name", r"(^|[._\-])(m_?name|middle_?name|middle_?initial|mi)([._\-]|$)",
     r"\b(middle (name|initial))\b"),
    ("last_name", r"(^|[._\-])(l_?name|last_?name|surname|family_?name)([._\-]|$)",
     r"\b(last|sur|family)\s*name\b"),
    ("prefix", r"(^|[._\-])(prefix|salutation|title_?prefix)([._\-]|$)",
     r"\b(prefix|salutation|title)\b"),
    ("suffix", r"(^|[._\-])(suffix|name_?suffix)([._\-]|$)", r"\bsuffix\b"),
    ("gender", r"(gender|sex)(?!ual)", r"\b(gender|sex)\b"),
    ("age", r"(^|[._\-])age([._\-]|$)", r"^\s*age\b"),
    # organisation before the generic name rules could claim "company name"
    ("company", r"(company|employer|organi[sz]ation|business_?name|firm)",
     r"\b(company|employer|organi[sz]ation|business|firm)\b"),
    ("job_title", r"(job_?title|occupation|position|role_?title)",
     r"\b(job title|occupation|position)\b"),
    ("department", r"(department|dept)", r"\b(department|dept)\b"),
    ("employee_id", r"(employee_?(id|number|no))", r"\bemployee (id|number)\b"),
    ("full_name", r"(^|[._\-])(full_?name|name|contact_?name|applicant_?name)([._\-]|$)",
     r"^\s*(full |contact |applicant )?name\s*\*?\s*$"),
    # contact
    ("email", r"(e_?mail)", r"\be-?mail\b"),
    ("phone_mobile", r"(mobile|cell)_?(phone|number|no)?",
     r"\b(mobile|cell)( phone| number)?\b"),
    ("phone", r"(phone|tel|fax|contact_?number)",
     r"\b(phone|telephone|tel|fax)\b"),
    ("url", r"(website|url|homepage|web_?site)", r"\b(web ?site|url|homepage)\b"),
    # address, specific parts before the street catch-all
    ("address_line2", r"(address_?(line_?)?2|addr2|apt|suite|unit)",
     r"\b(address (line )?2|apt|apartment|suite|unit)\b"),
    ("postal_code", r"(zip|postal|postcode|post_?code)",
     r"\b(zip( ?code)?|postal code|post ?code)\b"),
    ("city", r"(^|[._\-])(city|town|locality|municipality)([._\-]|$)",
     r"\b(city|town|locality)\b"),
    ("state", r"(^|[._\-])(state|province|region|st)([._\-]|$)",
     r"\b(state|province|region)\b"),
    ("country", r"(^|[._\-])country([._\-]|$)", r"\bcountry\b"),
    ("street_address", r"(street|address_?(line_?)?1|addr1|address)",
     r"\b(street|address( line 1)?|mailing address)\b"),
    # financial
    ("credit_card_number", r"(card_?(number|no)|cc_?num|credit_?card)",
     r"\b(card number|credit card)\b"),
    ("credit_card_expiry", r"(exp(iry|iration)?_?(date|month|year)?|cc_?exp)",
     r"\b(expir\w*)\b"),
    ("credit_card_cvv", r"(cvv|cvc|csc|security_?code)",
     r"\b(cvv|cvc|security code)\b"),
    ("routing_number", r"(routing|aba|sort_?code)", r"\b(routing|aba|sort code)\b"),
    ("iban", r"(iban)", r"\biban\b"),
    ("account_number", r"(account_?(number|no|num)|acct)",
     r"\b(account (number|no))\b"),
    ("currency_amount",
     r"(amount|price|cost|salary|income|balance|premium|deductible|total|fee"
     r"|(coverage|credit|policy|spending)_?limit)",
     r"\b(amount|price|cost|salary|income|balance|premium|deductible|total|fee)\b"),
    # insurance / claims
    ("policy_number", r"(policy_?(number|no|id)?)", r"\bpolicy( number)?\b"),
    ("claim_number", r"(claim_?(number|no|id)?)", r"\bclaim( number)?\b"),
    ("group_number", r"(group_?(number|no|id))", r"\bgroup (number|id)\b"),
    ("member_id", r"(member_?(id|number|no)|subscriber_?id)",
     r"\b(member|subscriber) (id|number)\b"),
    ("diagnosis_code", r"(diagnosis|icd|dx_?code)", r"\b(diagnosis|icd)\b"),
    # generic, last
    ("percentage", r"(percent|pct|rate)", r"\b(percent|%|rate)\b"),
    ("date", r"(date|_on$|_at$)", r"\bdate\b"),
]

# A specific meaning that is a special case of a more general one. When the
# control's own type says "this is a date" and the name says "this is a date of
# birth", they agree rather than compete, and the specific reading wins.
REFINES = {
    "date_of_birth": "date",
    "credit_card_expiry": "date",
    "phone_mobile": "phone",
    "credit_card_cvv": "integer",
    "postal_code": "integer",
    "age": "integer",
    "percentage": "decimal",
    "currency_amount": "decimal",
    "account_number": "integer",
    "routing_number": "integer",
}

_COMPILED = [
    (
        semantic,
        re.compile(name_pattern, re.I) if name_pattern else None,
        re.compile(label_pattern, re.I) if label_pattern else None,
    )
    for semantic, name_pattern, label_pattern in RULES
]

# Option sets whose shape gives the field away even when name and label do not.
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _strip_group_prefix(field: Field) -> str:
    """Drop a leading group name from a field name before matching.

    ``employer_city`` belongs to the ``employer`` group and asks for a city;
    without stripping, the generic "employer" keyword would claim it as a
    company name.
    """
    name = field.name or ""
    group = field.group
    if not group:
        return name
    for separator in ("_", ".", "-", ""):
        prefix = f"{group}{separator}"
        if len(name) > len(prefix) and name.lower().startswith(prefix.lower()):
            return name[len(prefix):]
    return name


def _match_rules(field: Field) -> tuple[str, float, list[str]] | None:
    name = _strip_group_prefix(field)
    label = field.label or ""
    for semantic, name_re, _ in _COMPILED:
        if name_re is not None and name_re.search(name):
            return semantic, WEIGHT_NAME, [f"name matches /{name_re.pattern}/"]
    for semantic, _, label_re in _COMPILED:
        if label_re is not None and label and label_re.search(label):
            return semantic, WEIGHT_LABEL, [f"label {label!r} matches /{label_re.pattern}/"]
    return None


def _from_options(field: Field) -> tuple[str, float, list[str]] | None:
    if not field.options:
        return None
    values = {o.value.strip().upper() for o in field.options}
    if len(values) >= 5 and values <= US_STATE_CODES:
        return "state", WEIGHT_OPTIONS, ["options are US state codes"]
    if {"YES", "NO"} <= values and len(values) <= 3:
        return "boolean", WEIGHT_OPTIONS, ["options are yes/no"]
    return None


def _weak_signals(field: Field) -> tuple[str, float, list[str]] | None:
    inputmode = (field.extra.get("inputmode") or "").lower()
    if inputmode == "email":
        return "email", WEIGHT_WEAK, ["inputmode=email"]
    if inputmode == "tel":
        return "phone", WEIGHT_WEAK, ["inputmode=tel"]
    placeholder = field.placeholder or ""
    if "@" in placeholder and "." in placeholder:
        return "email", WEIGHT_WEAK, [f"placeholder {placeholder!r} looks like an email"]
    if re.search(r"\d{3}[)\-\s]\s*\d{3}[\-\s]\d{4}", placeholder):
        return "phone", WEIGHT_WEAK, [f"placeholder {placeholder!r} looks like a phone number"]
    if re.fullmatch(r"[\s#]*\d{5}([\-\s]\d{4})?[\s#]*", placeholder.replace("X", "0")):
        return "postal_code", WEIGHT_WEAK, [f"placeholder {placeholder!r} looks like a ZIP"]
    return None


def _data_type_for(field: Field) -> str:
    semantic = field.semantic_type
    if semantic in ("date", "date_of_birth"):
        return "date"
    if semantic == "datetime":
        return "datetime"
    if semantic == "time":
        return "time"
    if semantic == "boolean":
        return "boolean"
    if semantic in ("age", "integer"):
        return "integer"
    if semantic in ("currency_amount", "decimal", "percentage"):
        return "number"
    if field.control == "number":
        # A number control whose step is a whole number holds an integer.
        step = field.constraints.step
        return "integer" if step is None or float(step).is_integer() else "number"
    return "string"


def infer_field(field: Field) -> Field:
    """Fill in ``semantic_type``, ``data_type``, ``confidence`` and evidence."""
    # A field spec that states its own meaning is authoritative; inference
    # exists to fill gaps, not to overrule the person who wrote it down.
    if field.semantic_type != "unknown" and field.confidence >= 1.0:
        field.data_type = field.data_type if field.data_type != "string" \
            else _data_type_for(field)
        return field

    candidates: list[tuple[str, float, list[str]]] = []

    token = (field.extra.get("autocomplete") or "").strip().lower()
    if token:
        # Tokens may carry a section or scope prefix: "shipping postal-code".
        parts = token.split()
        for part in reversed(parts):
            if part in AUTOCOMPLETE_MAP:
                candidates.append(
                    (AUTOCOMPLETE_MAP[part], WEIGHT_AUTOCOMPLETE, [f"autocomplete={token}"])
                )
                break

    # A control's own type is decisive for email/tel/url/date, but "number"
    # and "checkbox" only describe shape, so they must not outrank a name that
    # identifies the field precisely.
    # A control type that fully determines the meaning (email, url, checkbox)
    # is trusted outright. One that only narrows it to a family - "some date",
    # "some phone", "some number" - is kept low so a name rule can refine it.
    control_type = field.control
    if control_type in INPUT_TYPE_MAP:
        weight = WEIGHT_INPUT_TYPE
        if control_type in ("number", "range", "color", "date", "month", "tel"):
            weight = WEIGHT_WEAK
        candidates.append((INPUT_TYPE_MAP[control_type], weight, [f"input type={control_type}"]))

    for signal in (_match_rules(field), _from_options(field), _weak_signals(field)):
        if signal:
            candidates.append(signal)

    if not candidates:
        if field.options:
            field.semantic_type = "enum"
            field.confidence = WEIGHT_OPTIONS
            field.evidence = ["closed set of options, no other signal"]
        else:
            field.semantic_type = "free_text" if field.control == "textarea" else "unknown"
            field.confidence = 0.85 if field.control == "textarea" else 0.0
            field.evidence = (
                ["textarea control takes free-form prose"]
                if field.control == "textarea" else ["no matching signal"]
            )
        field.data_type = _data_type_for(field)
        return field

    # Promote a specific reading over the general one it refines, so the two
    # signals reinforce each other instead of the broader one winning on weight.
    present = {c[0] for c in candidates}
    promoted: list[tuple[str, float, list[str]]] = []
    for semantic, weight, evidence in candidates:
        general = REFINES.get(semantic)
        if general and general in present:
            general_weight = max(w for s, w, _ in candidates if s == general)
            weight = max(weight, general_weight)
            evidence = evidence + [f"refines {general}"]
        promoted.append((semantic, weight, evidence))
    candidates = promoted

    candidates.sort(key=lambda c: c[1], reverse=True)
    best_type, best_weight, evidence = candidates[0]

    # Independent signals that agree raise confidence; the cap keeps inference
    # honest about never being certain.
    agreeing = [
        c for c in candidates[1:]
        if c[0] == best_type or REFINES.get(best_type) == c[0]
    ]
    confidence = best_weight
    for _, weight, extra_evidence in agreeing:
        confidence = min(0.99, confidence + (1.0 - confidence) * weight * 0.5)
        evidence = evidence + extra_evidence

    disagreeing = [
        c for c in candidates[1:]
        if c[0] != best_type and REFINES.get(best_type) != c[0]
    ]
    for other_type, weight, _ in disagreeing[:2]:
        evidence.append(f"rejected {other_type} (weight {weight:.2f})")

    # A closed option set overrides the free-form reading of the same meaning:
    # a select labelled "State" must emit one of its own options.
    if field.options and best_type in ("free_text", "unknown"):
        best_type = "enum"

    field.semantic_type = best_type
    field.confidence = confidence
    field.evidence = evidence
    field.data_type = _data_type_for(field)
    return field


def infer(schema: FormSchema) -> FormSchema:
    """Run inference over every field of a schema, in place."""
    for field in schema.fields:
        infer_field(field)
    return schema
