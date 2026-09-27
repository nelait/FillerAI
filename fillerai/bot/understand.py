"""Reading a phrase: which template it means, and which values it carries.

This is the reading that is always there. It runs on this machine, in the
standard library, and needs no key; the language-model reading in
:mod:`fillerai.llm.understand` is an optional replacement that produces the
same :class:`Reading` and goes through the same :func:`accept` checks.

Intent is a score against each template's examples, name and field words.
It is not clever, and it does not have to be: a bot for one business has a
handful of templates, and "update my city" is not hard to tell from "send me
my policy documents". What matters more is that it *knows when it cannot
tell*, and says so with a choice rather than a guess.

Values are read by finding a field's words in the phrase - its label, its
aliases, and the defaults for its semantic type - and taking what follows
them, up to where the next field's words begin::

    please update my city from SFO to Irvine and house no 1429 Silverstein
                     ^^^^ ---------------------  ^^^^^^^^ ----------------
                     city   before SFO, after Irvine      street_address

Values with an unmistakable shape - a ZIP code, an email address, a state,
an option off a closed list - are also recognised with no label at all, but
only when exactly one field in the template could hold them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from .template import Template, TemplateField

#: Below this, a template is not what the person meant.
MATCH_AT = 0.34
#: Two templates closer than this are a question, not an answer.
AMBIGUOUS_WITHIN = 0.1
#: Longest value taken for a field. A sentence is not a street address.
MAX_VALUE = 120

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}

COUNTRY_WORDS: dict[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "united states of america": "US", "america": "US",
    "canada": "CA", "mexico": "MX", "uk": "GB", "united kingdom": "GB",
    "great britain": "GB", "britain": "GB", "england": "GB", "india": "IN",
    "australia": "AU", "germany": "DE", "france": "FR", "japan": "JP", "brazil": "BR",
}

_STOP = frozenset("""
a an the my our your his her their me i we you it its this that these those
to from of for in on at by with and or but so as is are was be been am
please pls kindly can could would will shall should want wanted like need needs
do does did just also too now new old some any hi hello hey thanks thank
""".split())

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_US_ZIP = re.compile(r"(?<![\d-])\d{5}(?:-\d{4})?(?![\d-])")
_CA_POSTAL = re.compile(r"\b[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")

_FROM_TO = re.compile(
    r"^(?:(?:is|was|currently)\s+)?from\s+(?P<old>.+?)\s+(?:to|into|->|=>)\s+(?P<new>.+)$",
    re.IGNORECASE)
_TO_FROM = re.compile(
    r"^(?:to\s+)?(?P<new>.+?)\s+(?:instead of|rather than|not)\s+(?P<old>.+)$",
    re.IGNORECASE)
_LEAD = re.compile(
    r"^(?:\s|[:=,\-]|\b(?:is|are|to|as|should be|will be|would be|now|of|no\.?|"
    r"number|num|#|new|my|the|be|changed to|change to|updated to|set to)\b)+",
    re.IGNORECASE)
_TRAIL = re.compile(
    r"(?:[\s,;.!?]|\b(?:and|also|plus|then|please|thanks|thank you|my|the|"
    r"our|with|new|as well|too)\b)+$",
    re.IGNORECASE)
#: "actually make it Tustin" - a correction to whatever was said last.
_CORRECTION = re.compile(
    r"^(?:no[,.!]?\s+|actually[,]?\s+|sorry[,]?\s+|oops[,]?\s+)*"
    r"(?:make it|change it to|it should be|it'?s|it is|use|i meant|i mean)\s+(?P<value>.+)$",
    re.IGNORECASE)

#: Whole phrases that are an action rather than information. Matched against
#: the phrase with politeness stripped, and only as the whole of it: "submit"
#: is an action, "submit a claim for my car" is not.
ACTION_PHRASES: dict[str, tuple[str, ...]] = {
    "submit": ("submit", "submit it", "submit this", "submit now", "send it",
               "send", "file it", "yes submit", "yes submit it", "submit the form",
               "submit the request", "send the request"),
    "fill_form": ("fill the form", "fill form", "fill it in", "fill it", "fill in the form",
                  "prefill", "prefill the form", "pre fill", "fill the form for me",
                  "fill the form for manual submission", "manual", "manually",
                  "i will do it myself", "i'll do it myself", "let me do it",
                  "let me check first", "i'll submit it myself", "i will submit it myself"),
    "cancel": ("cancel", "start over", "restart", "reset", "never mind", "nevermind",
               "forget it", "stop", "clear", "clear it", "cancel that", "cancel it"),
    "confirm": ("yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct",
                "confirm", "confirmed", "go ahead", "do it", "looks good",
                "that's right", "thats right", "that is right", "sounds good",
                "right", "perfect", "yes please", "please do"),
}
_POLITE = re.compile(r"\b(?:please|pls|thanks|thank you|now|then|ok|okay)\b[,.!]?",
                     re.IGNORECASE)


@dataclass
class Found:
    """One value read out of a phrase."""

    value: str
    #: What the person said the old value was: "from SFO to Irvine".
    said_before: str = ""
    #: ``labelled`` (after the field's words), ``shape`` (recognised with no
    #: label), ``expected`` (the answer to the question just asked),
    #: ``corrected`` ("make it Tustin"), ``llm``.
    how: str = "labelled"
    confidence: float = 0.9


@dataclass
class Problem:
    """Something the person said for a field that could not be used."""

    field: str
    said: str
    reason: str


@dataclass
class Reading:
    """What one phrase was taken to mean."""

    #: Every template's score, best first.
    scores: list[tuple[str, float]] = dc_field(default_factory=list)
    values: dict[str, Found] = dc_field(default_factory=dict)
    problems: list[Problem] = dc_field(default_factory=list)
    #: ``local`` or ``llm``: which reader produced this.
    how: str = "local"
    #: Set by a reader that decided the template itself.
    template: str | None = None

    def best(self) -> tuple[str | None, float]:
        return self.scores[0] if self.scores else (None, 0.0)


# ----------------------------------------------------------------------
# words
# ----------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _stem(word: str) -> str:
    for tail in ("ing", "es", "ed", "s"):
        if len(word) > len(tail) + 3 and word.endswith(tail):
            return word[: -len(tail)]
    return word


def content_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", _squash(text))
            if w not in _STOP and len(w) > 1}


def action_phrase(text: str) -> str | None:
    """``submit``, ``fill_form``, ``cancel``, ``confirm`` - or None."""
    plain = _POLITE.sub(" ", _squash(text))
    plain = re.sub(r"[^a-z' ]+", " ", plain)
    plain = " ".join(plain.split())
    if not plain:
        return None
    for action, phrases in ACTION_PHRASES.items():
        if plain in phrases:
            return action
    return None


# ----------------------------------------------------------------------
# intent
# ----------------------------------------------------------------------


def score(template: Template, text: str) -> float:
    """How well a phrase matches a template, from 0 to 1."""
    said = content_words(text)
    if not said:
        return 0.0
    best = 0.0
    for example in template.examples:
        wanted = content_words(example)
        if wanted:
            best = max(best, len(said & wanted) / len(wanted))
    named = content_words(template.name)
    if named:
        best = max(best, 0.85 * len(said & named) / len(named))
    described = content_words(template.description)
    if described:
        best = max(best, 0.6 * len(said & described) / len(described))
    # Naming the template's fields is evidence too, and it is how "my zip is
    # 92618 now" finds the address template with no verb in it at all.
    lowered = " " + _squash(text) + " "
    mentioned = sum(1 for f in template.fields
                    if any(_has_word(lowered, w) for w in f.words()[:6]))
    return min(1.0, best + min(0.3, 0.12 * mentioned))


def _has_word(padded_text: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])",
                     padded_text) is not None


def rank(templates: list[Template], text: str) -> list[tuple[str, float]]:
    scored = [(t.key, round(score(t, text), 3)) for t in templates]
    return sorted(scored, key=lambda kv: (-kv[1], kv[0]))


def decide(scores: list[tuple[str, float]]) -> tuple[str | None, list[str]]:
    """The template meant, or the few it could have been.

    Returns ``(key, [])`` for a clear answer, ``(None, [keys])`` when two or
    more are too close to call, and ``(None, [])`` when nothing matched.
    """
    live = [(k, s) for k, s in scores if s >= MATCH_AT]
    if not live:
        return None, []
    top = live[0][1]
    close = [k for k, s in live if s >= top - AMBIGUOUS_WITHIN]
    if len(close) > 1:
        return None, close[:4]
    return live[0][0], []


# ----------------------------------------------------------------------
# values
# ----------------------------------------------------------------------


def accept(field: TemplateField, raw: str) -> tuple[str | None, str]:
    """A value tidied for a field, or None and the reason it cannot be.

    Every reader's values go through this, the language model's included, so
    "what may go in a field" is decided in one place.
    """
    value = " ".join(str(raw or "").split()).strip(" ,;.!?\"'")
    if not value:
        return None, "nothing was said for it"
    if len(value) > MAX_VALUE:
        return None, "that is too long to be one value"

    kind = field.semantic_type
    if kind in ("city", "state", "country", "first_name", "last_name", "full_name",
                "middle_name") and not re.search(r"[A-Za-z]", value):
        return None, f"that does not look like a {field.label.lower() or kind}"
    if kind == "state":
        code = value.upper() if value.upper() in US_STATES else _STATE_BY_NAME.get(value.lower())
        if code:
            value = code
        elif re.search(r"\d", value) or len(value) > 40:
            return None, "that does not look like a state"
    elif kind == "country":
        value = COUNTRY_WORDS.get(value.lower(), value)
    elif kind == "postal_code":
        found = _US_ZIP.search(value) or _CA_POSTAL.search(value)
        if found:
            value = found.group(0).upper()
        elif not re.fullmatch(r"[A-Za-z0-9 -]{3,10}", value):
            return None, "that does not look like a postal code"
    elif kind == "email":
        found = _EMAIL.search(value)
        if not found:
            return None, "that does not look like an email address"
        value = found.group(0)
    elif kind in ("phone", "phone_mobile"):
        digits = re.sub(r"\D", "", value)
        if not 7 <= len(digits) <= 15:
            return None, "that does not look like a phone number"

    if field.options:
        option = field.option_for(value)
        if option is None:
            # A state typed in full against a list of codes, or the other way.
            if kind == "state":
                option = (field.option_for(US_STATES.get(value.upper(), ""))
                          or field.option_for(_STATE_BY_NAME.get(value.lower(), "")))
            if option is None:
                inside = [o for o in field.options
                          if len(o) > 2 and _has_word(" " + value.lower() + " ", o.lower())]
                option = inside[0] if len(inside) == 1 else None
        if option is None:
            shown = ", ".join(field.options[:6]) + (", ..." if len(field.options) > 6 else "")
            return None, f"it has to be one of {shown}"
        value = option
    return value, ""


def _mentions(template: Template, text: str) -> list[tuple[int, int, str]]:
    """Every place a field's words appear, non-overlapping, in order.

    A word two fields share ("number" for a phone and an account) is only
    taken when it is one field's own label; otherwise it says nothing about
    which field is meant.
    """
    owners: dict[str, set[str]] = {}
    for f in template.fields:
        for word in f.words():
            owners.setdefault(word, set()).add(f.name)
    labels = {_squash(f.label): f.name for f in template.fields}

    lowered = text.lower()
    hits: list[tuple[int, int, str]] = []
    for word in sorted(owners, key=lambda w: (-len(w), w)):
        names = owners[word]
        if len(names) > 1:
            if word in labels:
                names = {labels[word]}
            else:
                continue
        pattern = r"(?<![a-z0-9])" + r"\s+".join(map(re.escape, word.split())) + r"(?![a-z0-9])"
        for match in re.finditer(pattern, lowered):
            start, end = match.span()
            if any(s < end and start < e for s, e, _ in hits):
                continue
            hits.append((start, end, next(iter(names))))
    return sorted(hits)


def _clean(span: str) -> str:
    previous = None
    while previous != span:
        previous = span
        span = _LEAD.sub("", span)
        span = _TRAIL.sub("", span)
    return span.strip()


def read_values(template: Template, text: str, *, expects: str | None = None,
                last_field: str | None = None) -> tuple[dict[str, Found], list[Problem]]:
    """The values a phrase carries for one template."""
    values: dict[str, Found] = {}
    problems: list[Problem] = []
    by_name = {f.name: f for f in template.fields}

    def take(name: str, raw: str, how: str, confidence: float, before: str = "") -> bool:
        value, reason = accept(by_name[name], raw)
        if value is None:
            problems.append(Problem(name, raw.strip(), reason))
            return False
        values[name] = Found(value=value, said_before=before.strip(" ,.;"), how=how,
                             confidence=confidence)
        return True

    hits = _mentions(template, text)
    used: list[tuple[int, int]] = []
    for index, (start, end, name) in enumerate(hits):
        stop = hits[index + 1][0] if index + 1 < len(hits) else len(text)
        span = text[end:stop]
        # A comma ends a value: "apt 4B, Austin, Texas" is a unit and then
        # something else, which the shape pass below gets a look at.
        cut = re.search(r"[,;]", span)
        if cut and not _FROM_TO.match(_clean(span)):
            span = span[:cut.start()]
            stop = end + cut.start()
        cleaned = _clean(span)
        if not cleaned:
            continue
        pair = _FROM_TO.match(cleaned) or _TO_FROM.match(cleaned)
        if pair:
            old, new = _clean(pair.group("old")), _clean(pair.group("new"))
            if take(name, new, "labelled", 0.95, before=old):
                used.append((start, stop))
            continue
        if name in values:
            continue
        if take(name, cleaned, "labelled", 0.9):
            used.append((start, stop))

    # Nothing labelled at all: the phrase may be the answer to the question
    # just asked, or a correction to the value given last.
    if not hits:
        correction = _CORRECTION.match(text.strip())
        if correction and last_field in by_name:
            take(last_field, _clean(correction.group("value")), "corrected", 0.9)
            return values, problems
        if expects in by_name and not action_phrase(text):
            answer = _clean(text)
            if answer and len(answer.split()) <= 12:
                if take(expects, answer, "expected", 0.9):
                    return values, problems
                # Not an answer to the question, but maybe an answer to a
                # different one: "92618" when asked for the state is a ZIP.
                _by_shape(template, text, values)
                if values:
                    problems.clear()
                return values, problems

    # What is left over may still hold values with an unmistakable shape.
    rest = list(text)
    for start, stop in used:
        rest[start:stop] = " " * (stop - start)
    _by_shape(template, "".join(rest), values)
    return values, problems


def _by_shape(template: Template, text: str, values: dict[str, Found]) -> None:
    open_fields = [f for f in template.fields if f.name not in values]

    def only(kind_test) -> TemplateField | None:
        fitting = [f for f in open_fields if kind_test(f)]
        return fitting[0] if len(fitting) == 1 else None

    for pattern, kinds in ((_EMAIL, ("email",)),
                           (_US_ZIP, ("postal_code",)),
                           (_CA_POSTAL, ("postal_code",))):
        found = pattern.search(text)
        target = only(lambda f: f.semantic_type in kinds)
        if found and target is not None:
            value, _ = accept(target, found.group(0))
            if value:
                values[target.name] = Found(value, how="shape", confidence=0.8)
                text = text.replace(found.group(0), " ")
                open_fields.remove(target)

    state = only(lambda f: f.semantic_type == "state")
    said_state = None
    if state is not None:
        for name, code in sorted(_STATE_BY_NAME.items(), key=lambda kv: -len(kv[0])):
            found = re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])",
                              text.lower())
            if found:
                said_state = (code, found.group(0))
                break
        if said_state is None:
            # A code must be written in capitals, and sit where an address
            # puts one: before a ZIP, or at the end of a clause.
            for found in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2})(?=\s*(?:\d{5}|[,;.]|$))", text):
                if found.group(1) in US_STATES:
                    said_state = (found.group(1), found.group(1))
                    break
        if said_state is not None:
            value, _ = accept(state, said_state[0])
            if value:
                values[state.name] = Found(value, how="shape", confidence=0.75)
                open_fields.remove(state)
            else:
                said_state = None

    _address_parts(template, text, values, open_fields)
    if said_state is not None:
        # So "CA" the state is not read again as "CA" the country.
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(said_state[1]) + r"(?![A-Za-z0-9])",
                      " ", text, count=1, flags=re.IGNORECASE)

    # An option off a closed list, when only one field offers it. Short codes
    # must be written as they are on the list ("CA", not "ca"), because "in",
    # "or" and "me" are words first and states second.
    offered: dict[str, list[tuple[TemplateField, str]]] = {}
    for f in open_fields:
        for option in f.options:
            offered.setdefault(option.lower(), []).append((f, option))
    for key, owners in sorted(offered.items(), key=lambda kv: -len(kv[0])):
        if len(owners) != 1 or len(key) < 2:
            continue
        f, option = owners[0]
        if f.name in values:
            continue
        if len(option) <= 3:
            found = re.search(r"(?<![A-Za-z0-9])" + re.escape(option) + r"(?![A-Za-z0-9])", text)
        else:
            found = re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text.lower())
        if found:
            values[f.name] = Found(option, how="shape", confidence=0.75)


_STREET = re.compile(
    r"(?<![\w-])\d+[A-Za-z]?\s+(?:[A-Za-z0-9.'-]+\s+){0,4}?"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Boulevard|Ct|Court|"
    r"Way|Pl|Place|Ter|Terrace|Pkwy|Parkway|Cir|Circle|Hwy|Highway|Loop|Trail)\b\.?",
    re.IGNORECASE)


def _address_parts(template: Template, text: str, values: dict[str, Found],
                   open_fields: list[TemplateField]) -> None:
    """A street and a city written the way an address is written.

    "12 Main St, Austin, Texas 78701": a number and a street suffix is a
    street, and the words just before a recognised state are its city. Only
    for a template that has one field of each kind.
    """
    def one(kind: str) -> TemplateField | None:
        fitting = [f for f in open_fields if f.semantic_type == kind]
        return fitting[0] if len(fitting) == 1 else None

    street = one("street_address")
    if street is not None:
        found = _STREET.search(text)
        if found:
            value, _ = accept(street, found.group(0))
            if value:
                values[street.name] = Found(value, how="shape", confidence=0.75)
                open_fields.remove(street)
                text = text.replace(found.group(0), " , ")

    city = one("city")
    state_field = next((f for f in template.fields if f.semantic_type == "state"), None)
    if city is None or state_field is None or state_field.name not in values:
        return
    # The words right before the state, back to a comma or a connector.
    names = [re.escape(US_STATES.get(values[state_field.name].value, "")),
             re.escape(values[state_field.name].value)]
    pattern = (r"(?:^|[,;]|\bto\b|\bin\b)\s*(?P<city>[A-Za-z][A-Za-z .'-]{1,40}?)\s*,?\s+"
               r"(?:" + "|".join(n for n in names if n) + r")\b")
    found = re.search(pattern, text, re.IGNORECASE)
    if found:
        value, _ = accept(city, _clean(found.group("city")))
        if value and len(value.split()) <= 3:
            values[city.name] = Found(value, how="shape", confidence=0.7)
            open_fields.remove(city)


def read(templates: list[Template], active: Template | None, text: str, *,
         expects: str | None = None, last_field: str | None = None) -> Reading:
    """The whole local reading: every template's score, and the values.

    Values are read for the active template if there is one, else for the
    best-scoring one - the conversation decides whether that is a switch.
    """
    scores = rank(templates, text)
    reading = Reading(scores=scores, how="local")
    target = active
    if target is None:
        key, _ = decide(scores)
        target = next((t for t in templates if t.key == key), None)
    if target is not None:
        reading.values, reading.problems = read_values(
            target, text,
            expects=expects if target is active else None,
            last_field=last_field if target is active else None)
    return reading


def plain(values: dict[str, Any]) -> dict[str, str]:
    """``Found`` values as a flat mapping, for logging and tests."""
    return {k: (v.value if isinstance(v, Found) else str(v)) for k, v in values.items()}
