"""Turn an HTML page into a :class:`~fillerai.schema.FormSchema`.

Only structural work happens here: finding controls, pairing them with their
labels, grouping radios, and reading declared constraints. Deciding what a
field *means* is :mod:`fillerai.infer`'s job.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..schema import Constraints, Field, FormSchema, Option, Screen
from . import dom
from .dom import Node

# Controls that hold no user data.
NON_DATA_INPUT_TYPES = {"submit", "reset", "button", "image", "file"}

# Attributes commonly used to mark a step/page in a multi-screen flow.
SCREEN_ATTRS = ("data-screen", "data-step", "data-page")
SCREEN_CLASS_HINTS = ("screen", "step", "page", "wizard-step", "form-step")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_")


# --------------------------------------------------------------------------
# label / description association
# --------------------------------------------------------------------------


def _label_index(root: Node) -> dict[str, str]:
    """Map ``for`` target id -> label text."""
    index: dict[str, str] = {}
    for label in root.elements("label"):
        target = label.get("for")
        if target:
            text = label.inner_text()
            if text:
                index.setdefault(target, text)
    return index


def _id_text_index(root: Node) -> dict[str, str]:
    """Map element id -> its text, for resolving ``aria-describedby``."""
    return {
        node.get("id"): node.inner_text()
        for node in root.elements()
        if node.get("id") and node.inner_text()
    }


def _label_for(control: Node, labels: dict[str, str]) -> str | None:
    """Best available human caption for a control, most reliable source first."""
    control_id = control.get("id")
    if control_id and control_id in labels:
        return labels[control_id]

    wrapping = control.closest("label")
    if wrapping:
        text = wrapping.inner_text()
        if text:
            return text

    aria = control.get("aria-label")
    if aria and aria.strip():
        return aria.strip()

    # A label that simply precedes the control inside the same container, the
    # common shape when markup omits ``for``.
    parent = control.parent
    if parent is not None:
        preceding: str | None = None
        for sibling in parent.children:
            if sibling is control:
                return preceding
            if sibling.tag == "label":
                preceding = sibling.inner_text() or preceding
            elif not sibling.tag and sibling.text.strip():
                preceding = " ".join(sibling.text.split())
    return None


def _help_text_for(control: Node, id_text: dict[str, str]) -> str | None:
    described_by = control.get("aria-describedby")
    if described_by:
        parts = [id_text[token] for token in described_by.split() if token in id_text]
        if parts:
            return " ".join(parts)
    parent = control.parent
    if parent is not None:
        for sibling in parent.children:
            if sibling.tag in ("small", "span", "p") and (
                sibling.classes() & {"help", "hint", "help-text", "form-text", "description"}
            ):
                text = sibling.inner_text()
                if text:
                    return text
    return None


# --------------------------------------------------------------------------
# screens and groups
# --------------------------------------------------------------------------


def _screen_for(control: Node) -> tuple[str | None, str | None]:
    """Return ``(screen_id, screen_title)`` for the control, if any."""
    for ancestor in control.ancestors():
        for attr in SCREEN_ATTRS:
            value = ancestor.get(attr)
            if value:
                return slugify(value), _section_title(ancestor)
        if ancestor.tag in ("section", "div", "form"):
            if ancestor.classes() & set(SCREEN_CLASS_HINTS):
                identifier = ancestor.get("id") or _section_title(ancestor) or ancestor.tag
                return slugify(identifier), _section_title(ancestor)
    return None, None


def _section_title(node: Node) -> str | None:
    for child in node.children:
        if child.tag in ("legend", "h1", "h2", "h3", "h4"):
            text = child.inner_text()
            if text:
                return text
    for descendant in node.elements("legend", "h1", "h2", "h3", "h4"):
        text = descendant.inner_text()
        if text:
            return text
    return None


def _group_for(control: Node, name: str) -> str | None:
    """The coherence group a field belongs to.

    Fields in the same group share one consistent entity, so a billing city
    lines up with the billing state while shipping gets its own address.
    The enclosing fieldset wins when it declares a name, since that is the
    author's own grouping; otherwise a common prefix such as ``billing_zip``
    or ``shipping.city`` is used.
    """
    fieldset = control.closest("fieldset")
    if fieldset is not None:
        declared = fieldset.get("data-group") or fieldset.get("name")
        if declared:
            return slugify(declared)

    head = re.split(r"[._\-\[\]]+", name)[0].lower() if name else ""
    known_prefixes = {
        "billing", "shipping", "mailing", "home", "work", "primary", "secondary",
        "applicant", "coapplicant", "co", "spouse", "insured", "patient",
        "provider", "employer", "emergency", "beneficiary", "guarantor",
    }
    if head in known_prefixes:
        return head
    return None


# --------------------------------------------------------------------------
# constraints
# --------------------------------------------------------------------------


def _int_attr(node: Node, name: str) -> int | None:
    raw = node.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _float_attr(node: Node, name: str) -> float | None:
    raw = node.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _constraints_from(node: Node) -> Constraints:
    # ``min``/``max`` are dates on date inputs, so they are kept out of the
    # numeric bounds and preserved as-is in ``extra`` by the caller.
    numeric = node.get("type", "text") not in ("date", "datetime-local", "month", "week", "time")
    return Constraints(
        required=node.has("required") or node.get("aria-required") == "true",
        min_length=_int_attr(node, "minlength"),
        max_length=_int_attr(node, "maxlength"),
        pattern=node.get("pattern") or None,
        minimum=_float_attr(node, "min") if numeric else None,
        maximum=_float_attr(node, "max") if numeric else None,
        step=_float_attr(node, "step") if numeric else None,
        multiple=node.has("multiple"),
        read_only=node.has("readonly") or node.has("disabled"),
    )


def _extra_from(node: Node) -> dict[str, str]:
    extra: dict[str, str] = {}
    for attr in ("autocomplete", "inputmode", "role", "data-type", "data-format"):
        value = node.get(attr)
        if value:
            extra[attr] = value
    node_type = node.get("type", "text")
    if node_type in ("date", "datetime-local", "month", "week", "time"):
        for attr in ("min", "max"):
            value = node.get(attr)
            if value:
                extra[f"date_{attr}"] = value
    return extra


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _options_from_select(select: Node) -> list[Option]:
    options: list[Option] = []
    for option in select.elements("option"):
        value = option.get("value")
        text = option.inner_text()
        if value is None:
            value = text
        # A blank leading entry is a placeholder ("-- Select --"), not a choice.
        if value == "" :
            continue
        options.append(Option(value=value, label=text or value))
    return options


def _control_name(node: Node, seen: set[str], fallback_label: str | None) -> str:
    name = node.get("name") or node.get("id") or ""
    if not name and fallback_label:
        name = slugify(fallback_label)
    if not name:
        name = f"{node.get('type', node.tag)}_field"
    base, index = name, 2
    while name in seen:
        name = f"{base}_{index}"
        index += 1
    return name


def extract(html: str, name: str | None = None,
            source: dict | None = None) -> FormSchema:
    """Extract a schema from an HTML document."""
    root = dom.parse(html)
    labels = _label_index(root)
    id_text = _id_text_index(root)

    schema = FormSchema(name=name or "form", source=source or {"kind": "html"})
    screens: dict[str, Screen] = {}
    seen_names: set[str] = set()
    radio_groups: dict[str, Field] = {}

    for node in root.elements("input", "select", "textarea"):
        node_type = (node.get("type") or "text").lower()
        if node.tag == "input" and node_type in NON_DATA_INPUT_TYPES:
            continue
        if node.tag == "input" and node_type == "hidden":
            continue

        label = _label_for(node, labels)
        screen_id, screen_title = _screen_for(node)
        if screen_id and screen_id not in screens:
            screens[screen_id] = Screen(id=screen_id, title=screen_title)

        # Radios sharing a name are one field whose options are the choices.
        if node.tag == "input" and node_type == "radio":
            group_name = node.get("name") or slugify(label or "choice")
            existing = radio_groups.get(group_name)
            value = node.get("value") or slugify(label or "option")
            if existing is not None:
                existing.options.append(Option(value=value, label=label or value))
                existing.constraints.required = (
                    existing.constraints.required or node.has("required")
                )
                continue
            fieldset = node.closest("fieldset")
            caption = _section_title(fieldset) if fieldset is not None else None
            field = Field(
                name=group_name,
                label=caption,
                control="radio",
                screen=screen_id,
                group=_group_for(node, group_name),
                options=[Option(value=value, label=label or value)],
                constraints=Constraints(required=node.has("required")),
                help_text=_help_text_for(node, id_text),
                extra=_extra_from(node),
            )
            radio_groups[group_name] = field
            seen_names.add(group_name)
            schema.fields.append(field)
            continue

        field_name = _control_name(node, seen_names, label)
        seen_names.add(field_name)

        if node.tag == "select":
            control = "multiselect" if node.has("multiple") else "select"
            options = _options_from_select(node)
        elif node.tag == "textarea":
            control = "textarea"
            options = []
        elif node_type == "checkbox":
            control = "checkbox"
            options = []
        else:
            control = node_type
            options = []

        if node.tag == "input" and node.get("list"):
            # A datalist offers suggestions rather than a closed set; the
            # values are still useful as a sampling pool.
            datalist_id = node.get("list")
            for candidate in root.elements("datalist"):
                if candidate.get("id") == datalist_id:
                    options = _options_from_select(candidate)
                    break

        field = Field(
            name=field_name,
            label=label,
            control=control,
            screen=screen_id,
            group=_group_for(node, field_name),
            options=options,
            constraints=_constraints_from(node),
            placeholder=node.get("placeholder") or None,
            help_text=_help_text_for(node, id_text),
            extra=_extra_from(node),
        )
        schema.fields.append(field)

    schema.screens = list(screens.values())
    return schema


def extract_file(path: str | Path, name: str | None = None) -> FormSchema:
    path = Path(path)
    html = path.read_text(encoding="utf-8")
    return extract(
        html,
        name=name or slugify(path.stem) or "form",
        source={"kind": "html", "path": str(path)},
    )
