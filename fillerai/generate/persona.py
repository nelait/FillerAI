"""A coherent synthetic entity.

The unit of generation is not a field, it is a *persona*: one imaginary
person, their addresses, their employer, their account identifiers. Every
field in a record is rendered by asking the persona for the fact it wants.
Coherence falls out of that rather than being patched up afterwards, so a
city always sits in its own state, a phone's area code matches where the
person lives, and an email is built from the name above it.

Groups
------
A form often asks for the same kind of fact twice: a billing address and a
shipping address. Those are marked as separate ``group`` values during
extraction, and the persona keeps one address per group, so within a group
everything agrees while the groups stay independent.

Safety
------
By default identifiers come from ranges that cannot belong to a real person:
SSA has never issued an area number above 899, ``555-0100``-``555-0199`` is
reserved for fiction, and the email domains are the RFC 2606 documentation
domains. Card numbers are Luhn-valid so validators accept them, but use the
IIN ranges the networks publish as test values. Pass
``safe_identifiers=False`` when a downstream validator needs fully realistic
shapes and the output will not leave a controlled environment.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field as dc_field

from . import catalogs
from .catalogs import Place


@dataclass
class Address:
    place: Place
    street: str
    line2: str | None
    postal_code: str
    area_code: str

    @property
    def city(self) -> str:
        return self.place.city

    @property
    def state(self) -> str:
        return self.place.state

    @property
    def state_name(self) -> str:
        return self.place.state_name


@dataclass
class Employment:
    company: str
    job_title: str
    department: str
    employee_id: str


@dataclass
class Persona:
    """One coherent imaginary entity, lazily filled in as fields ask for it."""

    rng: random.Random
    safe_identifiers: bool = True
    today: dt.date = dc_field(default_factory=dt.date.today)

    # identity
    prefix: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    suffix: str = ""
    gender: str = ""
    date_of_birth: dt.date = dt.date(1990, 1, 1)

    _addresses: dict[str, Address] = dc_field(default_factory=dict)
    # group -> USPS codes the form will accept, set before any address for
    # that group is created. See ``constrain_states``.
    _allowed_states: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    # The group whose address ungrouped fields should use. A phone with no
    # group of its own belongs to the person, so its area code has to match
    # the address the form actually shows rather than a hidden extra one.
    _primary_group: str | None = None
    _employment: Employment | None = None
    _email: str | None = None
    _phones: dict[str, str] = dc_field(default_factory=dict)
    _identifiers: dict[str, str] = dc_field(default_factory=dict)
    _card: dict[str, str] = dc_field(default_factory=dict)
    _related: dict[str, "Persona"] = dc_field(default_factory=dict)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        rng = self.rng
        pool = catalogs.FIRST_NAMES_COMMON + catalogs.FIRST_NAMES_NEUTRAL
        self.first_name = rng.choice(pool)
        self.last_name = rng.choice(catalogs.LAST_NAMES)
        self.middle_name = rng.choice(pool) if rng.random() < 0.45 else ""
        self.suffix = rng.choice(catalogs.NAME_SUFFIXES) if rng.random() < 0.08 else ""
        # Gender is picked first so the honorific can agree with it. Given
        # names are deliberately not gender-coded: real populations do not
        # work that way, and baking the assumption in would teach a model a
        # correlation that is not there.
        self.gender = rng.choice(catalogs.GENDERS)
        self.prefix = (
            rng.choice(catalogs.PREFIXES_BY_GENDER[self.gender])
            if rng.random() < 0.3 else ""
        )
        # Adults, skewed the way a customer base actually is rather than flat.
        age = min(int(rng.triangular(18, 88, 41)), 88)
        self.date_of_birth = _birth_date_for_age(rng, age, self.today)

    # ------------------------------------------------------------------
    # other people on the same form
    # ------------------------------------------------------------------

    def related(self, key: str) -> "Persona":
        """A different person who appears on this form.

        A referring provider, a spouse or a beneficiary is not the subject of
        the record, so they get their own name, contact details and address
        rather than echoing the main persona back.
        """
        existing = self._related.get(key)
        if existing is not None:
            return existing
        # Seeded from this persona's own stream, so the whole record stays
        # reproducible from one seed.
        person = Persona(
            rng=random.Random(self.rng.getrandbits(64)),
            safe_identifiers=self.safe_identifiers,
            today=self.today,
        )
        # The form's own limits apply to everyone on it.
        person._allowed_states = dict(self._allowed_states)
        person._primary_group = self._primary_group
        self._related[key] = person
        return person

    # ------------------------------------------------------------------
    # names
    # ------------------------------------------------------------------

    @property
    def full_name(self) -> str:
        parts = [self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        parts.append(self.last_name)
        if self.suffix:
            parts.append(self.suffix)
        return " ".join(parts)

    @property
    def middle_initial(self) -> str:
        return f"{self.middle_name[0]}." if self.middle_name else ""

    @property
    def age(self) -> int:
        """Age in whole years, always consistent with ``date_of_birth``."""
        born, today = self.date_of_birth, self.today
        had_birthday = (today.month, today.day) >= (born.month, born.day)
        return today.year - born.year - (0 if had_birthday else 1)

    # ------------------------------------------------------------------
    # addresses
    # ------------------------------------------------------------------

    def constrain_states(self, group: str | None, states: tuple[str, ...]) -> None:
        """Limit a group's address to states the form actually offers.

        A ``<select>`` listing only a handful of states would otherwise be
        filled with an option that contradicts the city beside it. Callers
        apply this before the first ``address`` call for the group.
        """
        usable = tuple(s for s in states if any(p.state == s for p in catalogs.PLACES))
        if usable:
            self._allowed_states[group or ""] = usable

    def set_primary_address_group(self, group: str | None) -> None:
        """Name the group that owns the person's own address."""
        self._primary_group = group or ""

    def address(self, group: str | None = None) -> Address:
        """The address for a coherence group, created once and reused."""
        key = group or ""
        existing = self._addresses.get(key)
        if existing is not None:
            return existing
        # An ungrouped field defers to the primary address rather than
        # inventing a second place the record never shows.
        if key == "" and self._primary_group:
            return self.address(self._primary_group)

        rng = self.rng
        allowed = self._allowed_states.get(key)
        pool = catalogs.PLACES
        if allowed:
            pool = tuple(p for p in catalogs.PLACES if p.state in allowed) or pool
        place = rng.choice(pool)
        number = rng.randint(1, 9) * (10 ** rng.randint(0, 3)) + rng.randint(0, 99)
        street = (
            f"{number} {rng.choice(catalogs.STREET_NAMES)} "
            f"{rng.choice(catalogs.STREET_TYPES)}"
        )
        line2 = None
        if rng.random() < 0.3:
            line2 = f"{rng.choice(catalogs.SECONDARY_UNITS)} {rng.randint(1, 40)}{rng.choice('ABC ').strip()}"
        address = Address(
            place=place,
            street=street,
            line2=line2,
            postal_code=rng.choice(place.zip_codes),
            area_code=rng.choice(place.area_codes),
        )
        self._addresses[key] = address
        return address

    # ------------------------------------------------------------------
    # contact
    # ------------------------------------------------------------------

    def email(self) -> str:
        """An address built from this persona's own name, so the two agree."""
        if self._email is not None:
            return self._email
        rng = self.rng
        first = _ascii_token(self.first_name)
        last = _ascii_token(self.last_name)
        style = rng.randint(0, 4)
        if style == 0:
            local = f"{first}.{last}"
        elif style == 1:
            local = f"{first[0]}{last}"
        elif style == 2:
            local = f"{first}{last}{rng.randint(1, 99)}"
        elif style == 3:
            local = f"{first}_{last}"
        else:
            local = f"{last}.{first[0]}"
        self._email = f"{local}@{rng.choice(catalogs.EMAIL_DOMAINS)}"
        return self._email

    def phone(self, kind: str = "phone", group: str | None = None) -> str:
        """A number whose area code matches where this persona lives."""
        key = f"{kind}:{group or ''}"
        existing = self._phones.get(key)
        if existing is not None:
            return existing
        rng = self.rng
        area = self.address(group).area_code
        if self.safe_identifiers:
            # 555-0100..0199 is reserved for fictional use.
            exchange, line = "555", f"01{rng.randint(0, 99):02d}"
        else:
            exchange = str(rng.randint(200, 989))
            line = f"{rng.randint(0, 9999):04d}"
        number = f"{area}-{exchange}-{line}"
        self._phones[key] = number
        return number

    # ------------------------------------------------------------------
    # employment
    # ------------------------------------------------------------------

    def employment(self) -> Employment:
        if self._employment is None:
            rng = self.rng
            head = rng.choice(catalogs.COMPANY_HEADS)
            tail = rng.choice(catalogs.COMPANY_TAILS)
            suffix = rng.choice(catalogs.COMPANY_SUFFIXES)
            self._employment = Employment(
                company=f"{head} {tail} {suffix}",
                job_title=rng.choice(catalogs.JOB_TITLES),
                department=rng.choice(catalogs.DEPARTMENTS),
                employee_id=f"E{rng.randint(10000, 999999)}",
            )
        return self._employment

    # ------------------------------------------------------------------
    # identifiers
    # ------------------------------------------------------------------

    def identifier(self, kind: str) -> str:
        existing = self._identifiers.get(kind)
        if existing is not None:
            return existing
        rng = self.rng
        if kind == "ssn":
            # Area numbers 900-999 are never issued, so these cannot collide
            # with a real SSN while still passing shape validation.
            area = rng.randint(900, 999) if self.safe_identifiers else rng.choice(
                [n for n in range(1, 900) if n != 666]
            )
            value = f"{area:03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
        elif kind == "policy_number":
            value = f"{rng.choice(['POL', 'PL', 'P'])}-{rng.randint(100000, 999999)}"
        elif kind == "claim_number":
            year = self.today.year
            value = f"CLM{year}{rng.randint(100000, 999999)}"
        elif kind == "group_number":
            value = f"GRP{rng.randint(10000, 99999)}"
        elif kind == "member_id":
            value = f"{rng.choice(['MBR', 'SUB', 'ID'])}{rng.randint(1000000, 9999999)}"
        elif kind == "account_number":
            value = "".join(str(rng.randint(0, 9)) for _ in range(rng.choice([8, 10, 12])))
        elif kind == "routing_number":
            value = _routing_number(rng)
        elif kind == "iban":
            value = _iban(rng)
        else:
            value = f"{kind.upper()[:3]}{rng.randint(100000, 999999)}"
        self._identifiers[kind] = value
        return value

    def card(self, part: str) -> str:
        """Card number, expiry and CVV, all belonging to the same card."""
        if not self._card:
            rng = self.rng
            if self.safe_identifiers:
                # Network-published test IINs, Luhn-valid but non-issuable.
                prefix, length = rng.choice([("4111", 16), ("5555", 16), ("3782", 15)])
            else:
                prefix, length = rng.choice([("4", 16), ("52", 16), ("37", 15), ("6011", 16)])
            number = _luhn_complete(rng, prefix, length)
            expiry = self.today.replace(day=1) + dt.timedelta(days=rng.randint(60, 1400))
            self._card = {
                "number": number,
                "expiry": f"{expiry.month:02d}/{expiry.year % 100:02d}",
                "cvv": f"{rng.randint(0, 9999):04d}" if length == 15 else f"{rng.randint(0, 999):03d}",
            }
        return self._card[part]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _ascii_token(text: str) -> str:
    """Lowercase, ASCII-only form of a name, safe for an email local part."""
    return re.sub(r"[^a-z0-9]", "", text.lower()) or "user"


def _subtract_years(date: dt.date, years: int) -> dt.date:
    """``date`` shifted back whole years, moving 29 February to the 28th."""
    try:
        return date.replace(year=date.year - years)
    except ValueError:
        return date.replace(year=date.year - years, day=28)


def _birth_date_for_age(rng: random.Random, age: int, today: dt.date) -> dt.date:
    """A birth date whose age on ``today`` is exactly ``age``.

    Picking a birth *year* is not enough: someone born in December of the
    year ``today.year - 18`` has not turned 18 yet. The date is drawn from
    the window that actually yields the age asked for.
    """
    latest = _subtract_years(today, age)
    earliest = _subtract_years(today, age + 1) + dt.timedelta(days=1)
    span = (latest - earliest).days
    return earliest + dt.timedelta(days=rng.randint(0, max(span, 0)))


def _luhn_check_digit(digits: str) -> str:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def _luhn_complete(rng: random.Random, prefix: str, length: int) -> str:
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(length - len(prefix) - 1))
    return body + _luhn_check_digit(body)


def _routing_number(rng: random.Random) -> str:
    """Nine digits satisfying the ABA checksum, so validators accept it."""
    digits = [rng.randint(0, 9) for _ in range(8)]
    weights = (3, 7, 1, 3, 7, 1, 3, 7)
    total = sum(d * w for d, w in zip(digits, weights))
    digits.append((10 - total % 10) % 10)
    return "".join(str(d) for d in digits)


def _iban(rng: random.Random) -> str:
    """A GB IBAN with a correct mod-97 check, for forms that validate it."""
    bank = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
    sort_code = f"{rng.randint(0, 999999):06d}"
    account = f"{rng.randint(0, 99999999):08d}"
    body = f"{bank}{sort_code}{account}GB00"
    numeric = "".join(str(int(c, 36)) for c in body)
    check = 98 - int(numeric) % 97
    return f"GB{check:02d}{bank}{sort_code}{account}"
