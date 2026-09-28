"""A bot template: what one kind of request is made of.

"Change my address" is a street, a unit, a city, a state, a ZIP and a
country. "Send me a document" is a policy number, a document type and a name.
A template says so, together with a few phrasings a person might use to ask
for it, and that is all the bot needs to recognise the request and fill it in.

A template is deliberately smaller than a :class:`~fillerai.schema.FormSchema`.
A schema describes a form as it was found - controls, screens, constraints,
declared rules. A template describes a *request*, which is usually a slice of
one form, plus the words people use for it. So a template can be started from
a schema in the library (:func:`from_schema`) and then cut down, but it is not
one, and changing it never touches the schema.

``key`` is the stable name. A host application branches on it, and saving a
template whose key already exists replaces the old one - a host that
hard-coded ``address_change`` keeps working after somebody adds an alias.

The field ``name`` is the same join key it is everywhere else in AIrForms:
the name of the field in the host's form, in ``context.current``, in the
values the bot returns, and in a linked autofill model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from ..schema import SEMANTIC_TYPES, FormSchema
from ..store import StoreError

#: Bumped on a breaking change, exactly like ``SCHEMA_VERSION``.
TEMPLATE_VERSION = "1.0"

#: The two ways a conversation can finish.
FINISHING_ACTIONS = ("fill_form", "submit")

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")

MAX_FIELDS = 200
MAX_EXAMPLES = 100
MAX_ALIASES = 30
MAX_OPTIONS = 500
MAX_TEXT = 500

#: Words people use for a kind of field, beyond its label. The label and any
#: aliases a template declares are always added to these, so this table only
#: has to cover what someone would say without thinking about the form.
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name", "forename"),
    "middle_name": ("middle name",),
    "last_name": ("last name", "surname", "family name"),
    "full_name": ("name", "full name"),
    "date_of_birth": ("date of birth", "birthday", "dob", "born"),
    "gender": ("gender", "sex"),
    "email": ("email", "e-mail", "email address", "mail id"),
    "phone": ("phone", "phone number", "telephone", "number"),
    "phone_mobile": ("mobile", "cell", "cell phone", "mobile number"),
    "street_address": ("street address", "street", "address", "address line 1",
                       "house no", "house number", "house", "door number"),
    "address_line2": ("unit", "apt", "apartment", "suite", "flat", "address line 2"),
    "city": ("city", "town"),
    "state": ("state", "province"),
    "postal_code": ("zip", "zip code", "zipcode", "postal code", "postcode", "pin code"),
    "country": ("country",),
    "company": ("company", "employer", "organisation", "organization"),
    "job_title": ("job title", "title", "position", "role"),
    "department": ("department", "dept"),
    "employee_id": ("employee id", "employee number"),
    "account_number": ("account number", "account no", "account"),
    "routing_number": ("routing number",),
    "policy_number": ("policy number", "policy no", "policy", "policy id"),
    "claim_number": ("claim number", "claim no", "claim"),
    "group_number": ("group number", "group no"),
    "member_id": ("member id", "member number"),
    "date": ("date",),
}


class TemplateError(ValueError):
    """A template that cannot be used, and which part of it is wrong."""


@dataclass
class TemplateField:
    name: str
    label: str = ""
    semantic_type: str = "unknown"
    required: bool = False
    aliases: list[str] = dc_field(default_factory=list)
    #: A closed list. Empty means anything goes.
    options: list[str] = dc_field(default_factory=list)
    #: Shown in a question ("What is the new ZIP code? For example 92618").
    example: str = ""
    #: Fields this one depends on. When any of them changes in a
    #: conversation, the value on file for this one no longer holds: a new
    #: city means the old ZIP is wrong, so it is asked for, not carried over.
    follows: list[str] = dc_field(default_factory=list)

    def words(self) -> list[str]:
        """Everything a person might call this field, longest first.

        Longest first so "zip code" is found before "zip" and "street
        address" before "address" - the shorter one would otherwise take the
        match and leave "code" as the start of the value.
        """
        found: list[str] = []
        for word in [self.label, self.name.replace("_", " "), *self.aliases,
                     *DEFAULT_ALIASES.get(self.semantic_type, ())]:
            word = " ".join(str(word or "").lower().split())
            if word and word not in found:
                found.append(word)
        return sorted(found, key=lambda w: (-len(w), w))

    def option_for(self, value: str) -> str | None:
        """The option a value means, or None if it is not one of them.

        Case and spacing are forgiven, because "ca" typed and "CA" on the list
        are the same answer; anything further is not guessed at.
        """
        wanted = " ".join(str(value).lower().split())
        for option in self.options:
            if " ".join(option.lower().split()) == wanted:
                return option
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "label": self.label or self.name,
                               "semantic_type": self.semantic_type,
                               "required": self.required}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.options:
            out["options"] = list(self.options)
        if self.example:
            out["example"] = self.example
        if self.follows:
            out["follows"] = list(self.follows)
        return out

    @classmethod
    def from_dict(cls, data: Any, where: str = "field") -> "TemplateField":
        if not isinstance(data, dict):
            raise TemplateError(f"{where} must be an object")
        name = str(data.get("name") or "").strip()
        if not _NAME.match(name):
            raise TemplateError(
                f"{where} needs a name of letters, digits and _ (got {name!r})")
        semantic = str(data.get("semantic_type") or "unknown")
        if semantic not in SEMANTIC_TYPES:
            raise TemplateError(f"{name}: {semantic!r} is not a semantic type")
        aliases = _strings(data.get("aliases"), f"{name}.aliases", MAX_ALIASES)
        options = _strings(data.get("options"), f"{name}.options", MAX_OPTIONS,
                           option=True)
        return cls(
            name=name,
            label=_text(data.get("label")) or name.replace("_", " ").capitalize(),
            semantic_type=semantic,
            required=bool(data.get("required")),
            aliases=aliases,
            options=options,
            example=_text(data.get("example")),
            follows=_strings(data.get("follows"), f"{name}.follows", MAX_FIELDS),
        )


@dataclass
class Template:
    key: str
    name: str
    description: str = ""
    examples: list[str] = dc_field(default_factory=list)
    fields: list[TemplateField] = dc_field(default_factory=list)
    actions: list[str] = dc_field(default_factory=lambda: list(FINISHING_ACTIONS))
    #: A trained model whose fields share names with this template, used to
    #: complete what the person did not say. Optional.
    model_id: str | None = None
    done_message: str = ""
    template_version: str = TEMPLATE_VERSION
    #: The library entry this was read from. Not part of the template itself.
    entry_id: str | None = dc_field(default=None, compare=False)

    def field(self, name: str) -> TemplateField | None:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        return None

    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_version": self.template_version,
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "examples": list(self.examples),
            "fields": [f.to_dict() for f in self.fields],
            "actions": list(self.actions),
            "model_id": self.model_id,
            "done_message": self.done_message,
        }

    def card(self) -> dict[str, Any]:
        """The short description that goes in a listing."""
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "fields": len(self.fields),
            "required": [f.name for f in self.fields if f.required],
            "actions": list(self.actions),
            "model_id": self.model_id,
            "entry_id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Template":
        """Read and check a template, refusing rather than half-loading one."""
        if not isinstance(data, dict):
            raise TemplateError("a template must be a JSON object")
        version = str(data.get("template_version") or TEMPLATE_VERSION)
        if version.split(".")[0] != TEMPLATE_VERSION.split(".")[0]:
            raise TemplateError(
                f"this template is version {version}; this build reads "
                f"{TEMPLATE_VERSION}")
        key = str(data.get("key") or "").strip()
        if not _KEY.match(key):
            raise TemplateError(
                "a template needs a key of lower-case letters, digits and _, "
                f"starting with a letter (got {key!r})")
        raw_fields = data.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            raise TemplateError("a template needs at least one field")
        if len(raw_fields) > MAX_FIELDS:
            raise TemplateError(f"a template may have at most {MAX_FIELDS} fields")
        fields = [TemplateField.from_dict(f, f"field {i + 1}")
                  for i, f in enumerate(raw_fields)]
        seen: set[str] = set()
        for f in fields:
            if f.name in seen:
                raise TemplateError(f"two fields are called {f.name!r}")
            seen.add(f.name)
        for f in fields:
            for source in f.follows:
                if source not in seen or source == f.name:
                    raise TemplateError(
                        f"{f.name} follows {source!r}, which is not another field here")

        actions = data.get("actions", list(FINISHING_ACTIONS))
        if not isinstance(actions, list) or not actions or any(
                a not in FINISHING_ACTIONS for a in actions):
            raise TemplateError(
                "actions must list one or both of " + ", ".join(FINISHING_ACTIONS))
        model_id = data.get("model_id") or None
        if model_id is not None and not str(model_id).startswith("mdl-"):
            raise TemplateError(f"{model_id!r} is not a model id")
        return cls(
            key=key,
            name=_text(data.get("name")) or key.replace("_", " ").capitalize(),
            description=_text(data.get("description")),
            examples=_strings(data.get("examples"), "examples", MAX_EXAMPLES),
            fields=fields,
            actions=[a for a in FINISHING_ACTIONS if a in actions],
            model_id=str(model_id) if model_id else None,
            done_message=_text(data.get("done_message")),
            template_version=TEMPLATE_VERSION,
            entry_id=data.get("entry_id") or None,
        )


def _text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > MAX_TEXT:
        raise TemplateError(f"{text[:30]!r}... is longer than {MAX_TEXT} characters")
    return text


def _strings(value: Any, where: str, limit: int, option: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TemplateError(f"{where} must be a list")
    if len(value) > limit:
        raise TemplateError(f"{where} may have at most {limit} entries")
    out: list[str] = []
    for item in value:
        # An option may arrive as the schema writes one, {"value", "label"}.
        if option and isinstance(item, dict):
            item = item.get("value", item.get("label", ""))
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def from_schema(schema: FormSchema, key: str | None = None,
                fields: list[str] | None = None) -> Template:
    """A template with a schema's fields, to be cut down and given examples.

    Only fields a person could plausibly say are taken: a password, a free
    text box and a read-only field are left out, since the bot never asks
    for the first, cannot tidy the second and must not write the third.
    """
    skip = {"password", "free_text"}
    chosen = set(fields) if fields else None
    out: list[TemplateField] = []
    for f in schema.fields:
        if chosen is not None and f.name not in chosen:
            continue
        if chosen is None and (f.semantic_type in skip or f.constraints.read_only):
            continue
        if not _NAME.match(f.name):
            continue
        out.append(TemplateField(
            name=f.name,
            label=f.label or f.name,
            semantic_type=f.semantic_type if f.semantic_type in SEMANTIC_TYPES else "unknown",
            required=bool(f.constraints.required),
            options=[o.value for o in f.options if o.value][:MAX_OPTIONS],
        ))
    if not out:
        raise TemplateError("none of that schema's fields can go in a template")
    name = schema.name or "form"
    slug = re.sub(r"[^a-z0-9]+", "_", (key or name).lower()).strip("_") or "form"
    if not slug[0].isalpha():
        slug = "t_" + slug
    return Template(key=slug[:64], name=name.replace("_", " ").capitalize(),
                    examples=[], fields=out[:MAX_FIELDS])


#: Two ready-made templates, shipped inside the package so a new install has
#: something to talk to: an address change and a document request.
STARTERS_DIR = Path(__file__).resolve().parent / "starters"


def starters() -> dict[str, Template]:
    """The bundled starter templates, by key."""
    out: dict[str, Template] = {}
    for path in sorted(STARTERS_DIR.glob("*.template.json")):
        template = Template.from_dict(json.loads(path.read_text(encoding="utf-8")))
        out[template.key] = template
    return out


# ----------------------------------------------------------------------
# in the library
# ----------------------------------------------------------------------


def save(store: Any, template: Template, parent: str | None = None) -> Template:
    """Keep a template, replacing any other with the same key.

    The new one goes in before the old one comes out, so a failed write
    leaves the old template in place rather than neither.
    """
    old = [e for e in store.list(kind="template") if (e.meta or {}).get("key") == template.key]
    if parent is not None and not store.has(parent):
        parent = None
    entry = store.put("template", template.name, template.to_dict(), parent=parent,
                      meta={"key": template.key, "fields": len(template.fields),
                            "model_id": template.model_id})
    for stale in old:
        try:
            store.delete(stale.id)
        except StoreError:
            pass
    template.entry_id = entry.id
    return template


def listed(store: Any) -> list[Template]:
    """Every template in a library, newest version of each key, by name."""
    seen: dict[str, Template] = {}
    for entry in store.list(kind="template"):
        key = (entry.meta or {}).get("key")
        if not key or key in seen:
            continue
        try:
            seen[key] = load(store, entry.id)
        except (TemplateError, StoreError):
            continue
    return sorted(seen.values(), key=lambda t: t.name.lower())


def load(store: Any, entry_id: str) -> Template:
    body = dict(store.payload(entry_id))
    body["entry_id"] = entry_id
    return Template.from_dict(body)


def find(store: Any, key: str) -> Template | None:
    for entry in store.list(kind="template"):
        if (entry.meta or {}).get("key") == key:
            return load(store, entry.id)
    return None


def remove(store: Any, key: str) -> list[str]:
    gone: list[str] = []
    for entry in store.list(kind="template"):
        if (entry.meta or {}).get("key") == key:
            gone += store.delete(entry.id)
    return gone
