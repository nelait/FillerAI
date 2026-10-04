"""Real records: read a file, line its columns up with a form, and clean it.

Everything before this module invents its data, because a company that will
not hand over its submissions can still hand over its form. Sooner or later
real records do arrive, and they are wanted for two jobs: measuring a model
that was trained on invented ones, and training a model on the real thing
instead. Both need the same three steps first, and they live here so the UI,
the CLI and the tests all run the same ones.

1. **Read.** A CSV (any common delimiter), a JSON list of objects, or one
   object per line. Every cell comes out as text.
2. **Map.** Each column is matched to one of the form's fields by name or
   label, and the person checks the guess. A column that matches nothing is
   left out rather than forced onto a field.
3. **Clean.** A short list of named fixes, each of which can be turned off,
   and each of which reports what it changed. Nothing is fixed silently:
   cleaning real data is where a quiet rewrite does the most damage, so the
   report says how many cells every fix touched, with examples, and what is
   still wrong afterwards.

The cleaned records come out in the same shape generated ones have - one key
per field of the form, in the form's order, dates and numbers and options
written the way the generator writes them - so a model trained on invented
records can be scored on real ones, and every later stage accepts them
without knowing where they came from.

When there is no form at all, only a file, :func:`spec_from_table` writes a
field spec from the columns, and the ordinary spec reader and inference take
it from there.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field as dc_field
from typing import Any

from .generate.render import date_format
from .schema import Field, FormSchema

# Cells that mean "nothing was entered", however the export spelled it.
BLANK_WORDS = frozenset({
    "n/a", "na", "null", "none", "nil", "-", "--", "?", "undefined", "#n/a",
    "(blank)", "nan",
})
TRUE_WORDS = frozenset({"true", "yes", "y", "1", "on", "checked", "x", "t"})
FALSE_WORDS = frozenset({"false", "no", "n", "0", "off", "unchecked", "f"})

# The fixes, in the order they run, with what each is called on screen. The
# order matters: a value is trimmed before it is compared with anything, and
# only what survives every repair is judged invalid.
FIXES: tuple[tuple[str, str], ...] = (
    ("trim", "Trim stray spaces"),
    ("blanks", "Read N/A, null and - as empty"),
    ("case", "Even out ALL-CAPS names and addresses, lower-case emails"),
    ("options", "Match values to the form's options"),
    ("types", "Write numbers, dates and yes/no the form's way"),
    ("invalid", "Empty values the form would still reject"),
    ("duplicates", "Drop duplicate rows"),
    ("incomplete", "Drop rows missing a required field"),
)
FIX_NAMES = tuple(key for key, _ in FIXES)

# On unless turned off. The last two throw information away - a wrong value
# or a whole row - so they wait to be asked, with the report saying how much
# they would remove.
DEFAULT_FIXES = frozenset({"trim", "blanks", "case", "options", "types",
                           "duplicates"})

# How many before/after pairs a fix keeps to show, and how many values each
# remaining problem quotes.
EXAMPLES = 4

# Column-name similarity at which a column is taken to be a field.
MATCH_CUTOFF = 0.82

# A file wider than this is not a form export.
MAX_COLUMNS = 500

# The date shapes read, the form's own format always tried first. Month
# before day, as the generator writes them; day-first is tried only when
# month-first cannot be right.
DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d",
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
)

DATE_SEMANTICS = frozenset({"date", "datetime", "date_of_birth"})
DATE_CONTROLS = frozenset({"date", "datetime-local"})
NUMBER_SEMANTICS = frozenset({"integer", "decimal", "currency_amount", "percentage"})
# Fields an old system may have written in capitals, where "AUSTIN" and
# "Austin" are one value to a person and two to a model.
CAPITALISED = frozenset({
    "first_name", "middle_name", "last_name", "full_name", "street_address",
    "city", "company", "job_title", "department",
})


class DataError(ValueError):
    """The file cannot be read as records, or the mapping cannot be used."""


# ----------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------


@dataclass
class Table:
    """A file's rows, every cell as the text it was written as."""

    columns: list[str]
    rows: list[dict[str, str]]
    format: str

    def preview(self, count: int = 20) -> list[dict[str, str]]:
        return self.rows[:count]


def cell(value: Any) -> str:
    """One value as text, the way the rest of the project writes it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "|".join(cell(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def read_table(text: str, filename: str = "") -> Table:
    """Read a CSV, a JSON list of records, or NDJSON.

    The file's name decides where it can; otherwise the first character
    does, because a JSON export and a CSV are never confused for long.
    """
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise DataError("the file is empty")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    first = text.lstrip()[:1]

    if suffix in ("ndjson", "jsonl"):
        return _from_objects(_ndjson(text), "ndjson")
    if suffix == "json" or (suffix not in ("csv", "tsv", "txt") and first in "[{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if first == "{":
                return _from_objects(_ndjson(text), "ndjson")
            raise DataError("that file is not valid JSON") from None
        if isinstance(data, dict):
            data = data.get("records", data.get("rows"))
        if not isinstance(data, list):
            raise DataError("a JSON file needs a list of records, "
                            "or an object with a \"records\" list")
        return _from_objects(data, "json")
    return _csv(text, "\t" if suffix == "tsv" else None)


def _ndjson(text: str) -> list[Any]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            raise DataError(f"line {number} is not valid JSON") from None
    return out


def _from_objects(items: list[Any], fmt: str) -> Table:
    columns: list[str] = []
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise DataError(f"record {number} is not an object of field names to values")
        row = {}
        for key, value in item.items():
            key = str(key)
            if key.startswith("_"):
                continue  # bookkeeping, like a generated record's _persona
            if key not in seen:
                seen.add(key)
                columns.append(key)
            row[key] = cell(value)
        rows.append(row)
    _check_width(columns)
    return Table(columns=columns, rows=rows, format=fmt)


def _csv(text: str, delimiter: str | None) -> Table:
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        if header is None:
            header = _header(values)
            continue
        values = values + [""] * (len(header) - len(values))
        rows.append(dict(zip(header, values)))
    if header is None:
        raise DataError("the file has no header row")
    _check_width(header)
    return Table(columns=header, rows=rows, format="csv")


def _header(values: list[str]) -> list[str]:
    """Column names, made unique: a blank or repeated header still names a column."""
    out: list[str] = []
    for number, value in enumerate(values, 1):
        name = value.strip() or f"column_{number}"
        base, n = name, 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        out.append(name)
    return out


def _check_width(columns: list[str]) -> None:
    if not columns:
        raise DataError("no columns were found in that file")
    if len(columns) > MAX_COLUMNS:
        raise DataError(f"that file has {len(columns)} columns; "
                        f"more than {MAX_COLUMNS} is not a form export")


# ----------------------------------------------------------------------
# mapping columns to fields
# ----------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def fillable(schema: FormSchema) -> list[Field]:
    """The fields a record carries: everything but read-only ones."""
    return [f for f in schema.fields if not f.constraints.read_only]


def suggest_mapping(schema: FormSchema, columns: list[str]) -> dict[str, str]:
    """Which field each column most likely is, or ``""`` for none.

    Exact matches on name or label first, so a close-but-wrong guess cannot
    take a field an exact match wanted; then near matches for the columns
    left. A field is given to one column at most.
    """
    keys: dict[str, str] = {}
    for f in fillable(schema):
        for text in (f.name, f.label or ""):
            k = _key(text)
            if k:
                keys.setdefault(k, f.name)

    mapping = {column: "" for column in columns}
    taken: set[str] = set()
    for column in columns:
        name = keys.get(_key(column))
        if name and name not in taken:
            mapping[column] = name
            taken.add(name)
    for column in columns:
        if mapping[column] or not _key(column):
            continue
        free = [k for k, name in keys.items() if name not in taken]
        near = difflib.get_close_matches(_key(column), free, n=1, cutoff=MATCH_CUTOFF)
        if near:
            mapping[column] = keys[near[0]]
            taken.add(keys[near[0]])
    return mapping


def _complete(schema: FormSchema, columns: list[str],
              given: dict[str, str] | None) -> dict[str, str]:
    """The caller's mapping, with the guess filling in columns it did not name.

    A column named with ``""`` stays out. A guess never takes a field the
    caller already gave to another column.
    """
    given = {str(k): str(v or "") for k, v in (given or {}).items()}
    chosen = {v for v in given.values() if v}
    out = {}
    for column, guess in suggest_mapping(schema, columns).items():
        if column in given:
            out[column] = given[column]
        else:
            out[column] = guess if guess not in chosen else ""
    return out


def check_mapping(schema: FormSchema, columns: list[str],
                  mapping: dict[str, str]) -> dict[str, str]:
    """The mapping, refused if it names a field twice or one the form lacks."""
    names = {f.name for f in fillable(schema)}
    out: dict[str, str] = {}
    used: dict[str, str] = {}
    for column in columns:
        target = str(mapping.get(column) or "")
        if not target:
            out[column] = ""
            continue
        if target not in names:
            raise DataError(f"column {column!r} is mapped to {target!r}, "
                            "which this form does not have")
        if target in used:
            raise DataError(f"columns {used[target]!r} and {column!r} are both "
                            f"mapped to {target!r}; pick one")
        used[target] = column
        out[column] = target
    if not used:
        raise DataError("no column is mapped to a field of this form")
    return out


# ----------------------------------------------------------------------
# the fixes, one cell at a time
# ----------------------------------------------------------------------


def is_date(f: Field) -> bool:
    return f.control in DATE_CONTROLS or f.semantic_type in DATE_SEMANTICS \
        or f.data_type in ("date", "datetime")


def is_number(f: Field) -> bool:
    return not f.options and (f.data_type in ("integer", "number")
                              or f.semantic_type in NUMBER_SEMANTICS)


def is_boolean(f: Field) -> bool:
    return not f.options and (f.data_type == "boolean" or f.control == "checkbox")


def _trim(text: str) -> str:
    # Runs of spaces collapse; line breaks stay, because a long answer has them.
    return "\n".join(re.sub(r"[ \t ]+", " ", line).strip()
                     for line in text.strip().splitlines())


def _case(f: Field, text: str) -> str:
    if f.semantic_type == "email":
        return text.lower()
    if f.semantic_type in CAPITALISED and not f.options and text.isupper() \
            and sum(ch.isalpha() for ch in text) >= 3:
        # Word by word, leaving anything with a digit in it alone: "APT 7B"
        # is a unit, not a name. Letters either side of an apostrophe or a
        # hyphen each get their capital, so O'BRIEN comes back O'Brien.
        return " ".join(
            w if any(ch.isdigit() for ch in w)
            else re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), w)
            for w in text.split(" "))
    return text


def _option(f: Field, text: str) -> str:
    """The option a value names, matched without case or punctuation."""
    target = _key(text)
    if not target:
        return text
    for option in f.options:
        if option.value == text:
            return text
    for option in f.options:
        if _key(option.value) == target or _key(option.label or "") == target:
            return option.value
    return text


def _options(f: Field, text: str) -> str:
    if not f.options or not text:
        return text
    if f.constraints.multiple:
        parts = [p.strip() for p in re.split(r"[|;,]", text) if p.strip()]
        return "|".join(_option(f, p) for p in parts)
    return _option(f, text)


def parse_number(text: str) -> float | None:
    """A number as an export writes one: currency, separators and all."""
    raw = text.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    raw = re.sub(r"[\s$€£¥,%()]", "", raw)
    if raw.lower().startswith(("usd", "eur", "gbp")):
        raw = raw[3:]
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return -value if negative else value


def parse_date(text: str, first: str | None = None) -> dt.datetime | None:
    raw = text.strip()
    for fmt in ((first,) if first else ()) + DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _types(f: Field, text: str) -> Any:
    if not text:
        return text
    if is_boolean(f):
        word = text.lower()
        if word not in TRUE_WORDS | FALSE_WORDS:
            return text
        flag = word in TRUE_WORDS
        # A checkbox submits a tick; a yes/no box written as text says so.
        if f.control == "checkbox":
            return flag
        return "Yes" if flag else "No"
    if is_number(f):
        value = parse_number(text)
        if value is None:
            return text
        if f.data_type == "integer" or f.semantic_type == "integer":
            return str(int(value)) if value.is_integer() else text
        # Two places, as the generator writes a decimal, so "12.5" and
        # "12.50" are one value to the model rather than two.
        return f"{value:.2f}"
    if is_date(f) and f.control not in ("time", "month"):
        fmt = date_format(f)
        moment = parse_date(text, fmt)
        return moment.strftime(fmt) if moment else text
    return text


def problem(f: Field, value: Any) -> str | None:
    """Why the form would refuse this value, or None if it would take it."""
    if isinstance(value, bool) or value in ("", None):
        return None
    text = str(value)
    c = f.constraints
    if f.options:
        allowed = {o.value for o in f.options}
        parts = text.split("|") if c.multiple else [text]
        if any(p not in allowed for p in parts):
            return "not one of its options"
    if is_boolean(f) and text.lower() not in TRUE_WORDS | FALSE_WORDS:
        return "not a yes or no"
    if is_number(f):
        number = parse_number(text)
        if number is None or text != text.strip() or re.search(r"[^\d.\-]", text):
            return "not a number"
        if (f.data_type == "integer" or f.semantic_type == "integer") \
                and not number.is_integer():
            return "not a whole number"
        if c.minimum is not None and number < c.minimum:
            return "below the minimum"
        if c.maximum is not None and number > c.maximum:
            return "above the maximum"
    if is_date(f) and f.control not in ("time", "month"):
        fmt = date_format(f)
        try:
            dt.datetime.strptime(text, fmt)
        except ValueError:
            return "not a date in the form's format"
    if c.max_length is not None and len(text) > c.max_length:
        return "too long"
    if c.pattern:
        try:
            if not re.fullmatch(c.pattern, text):
                return "does not match the form's pattern"
        except re.error:
            pass
    return None


def _blank(f: Field, text: str) -> str:
    if text.lower() not in BLANK_WORDS:
        return text
    # "N/A" is an answer where the form offers it as one.
    if f.options and _option(f, text) != text or any(o.value == text for o in f.options):
        return text
    return ""


# ----------------------------------------------------------------------
# cleaning
# ----------------------------------------------------------------------


@dataclass
class FixCount:
    key: str
    label: str
    on: bool
    count: int = 0  # cells changed, or for the row fixes, rows dropped
    examples: list[dict[str, str]] = dc_field(default_factory=list)

    def note(self, field: str, before: Any, after: Any) -> None:
        self.count += 1
        if len(self.examples) < EXAMPLES:
            self.examples.append({"field": field, "before": cell(before),
                                  "after": cell(after)})

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "on": self.on,
                "count": self.count, "examples": self.examples,
                "unit": "rows" if self.key in ("duplicates", "incomplete") else "cells"}


@dataclass
class Cleaned:
    """The records, and an account of what was done to get them."""

    records: list[dict[str, Any]]
    rows_in: int
    fixes: list[FixCount]
    empty_rows: int
    mapping: list[dict[str, Any]]
    missing: list[str]
    problems: list[dict[str, Any]]
    truncated: int = 0

    @property
    def rows_out(self) -> int:
        return len(self.records)

    def headline(self) -> str:
        changed = sum(f.count for f in self.fixes if f.on and f.key not in
                      ("duplicates", "incomplete"))
        dropped = self.rows_in - self.rows_out
        left = sum(p["count"] for p in self.problems)
        parts = [f"{self.rows_out} of {self.rows_in} rows kept",
                 f"{changed} cell(s) fixed"]
        if dropped:
            parts.append(f"{dropped} row(s) dropped")
        parts.append(f"{left} value(s) the form would still reject" if left
                     else "every value fits the form")
        return ", ".join(parts)

    def report(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "empty_rows": self.empty_rows,
            "truncated": self.truncated,
            "fixes": [f.to_dict() for f in self.fixes],
            "mapping": self.mapping,
            "missing": self.missing,
            "problems": self.problems,
            "headline": self.headline(),
        }


def clean(schema: FormSchema, table: Table, mapping: dict[str, str] | None = None,
          fixes: set[str] | frozenset[str] | None = None,
          limit: int | None = None) -> Cleaned:
    """Turn a table into records for ``schema``, fixing what ``fixes`` names.

    A fix that is off still counts what it would have changed, so the report
    can say "12 rows miss a required field" before anybody decides to drop
    them. The records hold every fillable field of the form, blank where the
    file had no column for it, which is the shape a generated record has.
    """
    on = DEFAULT_FIXES if fixes is None else frozenset(fixes)
    unknown = on - set(FIX_NAMES)
    if unknown:
        raise DataError(f"there is no fix called {sorted(unknown)[0]!r}")
    mapping = _complete(schema, table.columns, mapping)
    mapping = check_mapping(schema, table.columns, mapping)

    fields = fillable(schema)
    by_name = {f.name: f for f in fields}
    source = {target: column for column, target in mapping.items() if target}
    counts = {key: FixCount(key, label, key in on) for key, label in FIXES}

    def step(key: str, f: Field, value: Any, repair) -> Any:
        if isinstance(value, bool):
            return value
        after = repair(f, value)
        if after == value:
            return value
        # "true" read back as a yes is a change of type, not of value, and
        # counting it would bury the fixes anybody needs to look at.
        if cell(after) != cell(value):
            counts[key].note(f.name, value, after)
        return after if key in on else value

    records: list[dict[str, Any]] = []
    empty_rows = 0
    for row in table.rows:
        record: dict[str, Any] = {}
        for f in fields:
            column = source.get(f.name)
            value: Any = cell(row.get(column)) if column else ""
            value = step("trim", f, value, lambda _f, v: _trim(v))
            value = step("blanks", f, value, _blank)
            value = step("case", f, value, _case)
            value = step("options", f, value, _options)
            value = step("types", f, value, _types)
            value = step("invalid", f, value,
                         lambda f_, v: "" if problem(f_, v) else v)
            record[f.name] = value
        if not any(record[name] not in ("", None) for name in source):
            empty_rows += 1
            continue
        records.append(record)

    kept: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    required = [f.name for f in fields if f.constraints.required and f.name in source]
    for record in records:
        key = tuple(cell(record[f.name]) for f in fields)
        if key in seen:
            counts["duplicates"].note("", "duplicate row", "dropped")
            if "duplicates" in on:
                continue
        seen.add(key)
        gaps = [name for name in required if record[name] in ("", None)]
        if gaps:
            counts["incomplete"].note(gaps[0], "empty", "row dropped")
            if "incomplete" in on:
                continue
        kept.append(record)

    truncated = 0
    if limit is not None and len(kept) > limit:
        truncated = len(kept) - limit
        kept = kept[:limit]

    return Cleaned(
        records=kept,
        rows_in=len(table.rows),
        fixes=[counts[key] for key in FIX_NAMES],
        empty_rows=empty_rows,
        mapping=_mapping_summary(table, mapping, by_name),
        missing=[f.name for f in fields if f.name not in source],
        problems=_problems(fields, kept),
        truncated=truncated,
    )


def _mapping_summary(table: Table, mapping: dict[str, str],
                     by_name: dict[str, Field]) -> list[dict[str, Any]]:
    out = []
    for column in table.columns:
        target = mapping.get(column, "")
        values = [cell(r.get(column)).strip() for r in table.rows]
        filled = [v for v in values if v]
        out.append({
            "column": column,
            "field": target,
            "label": (by_name[target].label or target) if target else "",
            "filled": len(filled),
            "distinct": len(set(filled)),
            "sample": [v for v, _ in Counter(filled).most_common(3)],
        })
    return out


def _problems(fields: list[Field], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What is still wrong, one line per field and kind rather than per cell."""
    found: dict[tuple[str, str], list] = {}
    for record in records:
        for f in fields:
            why = problem(f, record.get(f.name))
            if why:
                entry = found.setdefault((f.name, why), [0, []])
                entry[0] += 1
                value = cell(record.get(f.name))
                if len(entry[1]) < EXAMPLES and value not in entry[1]:
                    entry[1].append(value)
    return [{"field": name, "kind": why, "count": count, "examples": examples}
            for (name, why), (count, examples) in
            sorted(found.items(), key=lambda item: -item[1][0])]


# ----------------------------------------------------------------------
# a form from the file, when there is no form
# ----------------------------------------------------------------------

# A column is a choice when it repeats a few values rather than holding a
# new one in every row.
MAX_CHOICES = 20


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def spec_from_table(table: Table, name: str = "form") -> dict[str, Any]:
    """A field spec read off the columns: a name, a control, and options.

    Deliberately shallow. It decides only what the values themselves prove -
    that a column is yes/no, a date, a number, or a short list of choices -
    and leaves what each field *means* to inference, which reads names and
    labels far better than a pass over values could. The spec is an ordinary
    one, so it can be downloaded, corrected and read back like any other.
    """
    fields: list[dict[str, Any]] = []
    used: set[str] = set()
    for number, column in enumerate(table.columns, 1):
        base = slug(column) or f"column_{number}"
        field_name, n = base, 2
        while field_name in used:
            field_name, n = f"{base}_{n}", n + 1
        used.add(field_name)
        values = [_trim(cell(r.get(column))) for r in table.rows]
        filled = [v for v in values if v and v.lower() not in BLANK_WORDS]
        fields.append({"name": field_name, "label": column, **_shape(filled)})
    return {"name": slug(name) or "form", "fields": fields}


# Rows looked at to decide a column's type. Enough to be sure, few enough
# that a wide file does not spend seconds trying every date format on it.
SHAPE_SAMPLE = 400


def _shape(every: list[str]) -> dict[str, Any]:
    if not every:
        return {}
    values = every[:SHAPE_SAMPLE]
    lowered = {v.lower() for v in values}
    if lowered <= TRUE_WORDS | FALSE_WORDS and len(lowered) <= 2 \
            and lowered & TRUE_WORDS:
        return {"control": "checkbox", "data_type": "boolean"}
    if all(parse_date(v) for v in values) and not all(v.isdigit() for v in values):
        return {"control": "date", "data_type": "date"}
    digits = all(v.isdigit() for v in values)
    # Digits that are an identifier, not an amount: a leading zero, or the
    # same long length every time - postcodes, phone numbers, accounts.
    identifier = digits and (any(len(v) > 1 and v.startswith("0") for v in values)
                             or (len({len(v) for v in values}) == 1
                                 and len(values[0]) >= 5))
    numbers = [parse_number(v) for v in values]
    if not identifier and all(n is not None for n in numbers):
        whole = all(n.is_integer() for n in numbers) and not any("." in v for v in values)
        return {"control": "number", "data_type": "integer" if whole else "number"}
    if max(len(v) for v in every) > 120:
        return {"control": "textarea"}
    counts = Counter(every)
    if len(counts) <= MAX_CHOICES and len(counts) * 2 <= len(every):
        return {"control": "select",
                "options": [v for v, _ in counts.most_common()]}
    return {}
