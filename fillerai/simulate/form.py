"""Replaying the form the schema came from.

The schema already carries everything a form is: its screens, its fields in
order, their controls, their options and their constraints. So the simulator
rebuilds the form from the schema rather than re-showing the markup it was
extracted from.

That is a deliberate choice and it costs something, so here is the reasoning.
Putting the original markup back on screen would keep its exact styling, but
the markup is pasted by the user and putting it into the page means running
whatever came with it, and a sandbox strict enough to be safe is also strict
enough that the simulator could not type into it. Rebuilding also means the
Simulate stage works identically for a form that arrived as a field spec,
which is the case that exists precisely because the real markup could not be
shared. What is replayed is the form's substance - the same fields, in the
same order, on the same screens, with the same dropdowns and the same limits
- drawn by this application rather than by the source page.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from ..schema import Field, FormSchema

# Input types a browser has a control for. Anything else is typed into a box,
# which is what a browser does with an unknown type anyway.
NATIVE_INPUTS = frozenset({
    "text", "email", "tel", "url", "number", "date", "datetime-local", "time",
    "month", "week", "password", "search", "color", "range",
})

# Fields the agent never fills: the form computes or displays them.
def _is_fillable(field: Field) -> bool:
    return not field.constraints.read_only


@dataclass
class Control:
    """One field as something a browser can draw and a person can fill."""

    name: str
    label: str
    control: str  # select | multiselect | radio | checkbox | textarea | <input type>
    input_type: str = "text"  # for the input controls, the type attribute
    options: list[dict[str, str]] = dc_field(default_factory=list)
    placeholder: str | None = None
    help_text: str | None = None
    required: bool = False
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    pattern: str | None = None
    group: str | None = None
    semantic_type: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "control": self.control,
            "input_type": self.input_type,
            "required": self.required,
            "semantic_type": self.semantic_type,
        }
        if self.options:
            out["options"] = self.options
        for key in ("placeholder", "help_text", "max_length", "minimum",
                    "maximum", "step", "pattern", "group"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class Section:
    """Fields that belong together - one coherence group on one screen."""

    group: str | None
    title: str
    controls: list[Control] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "title": self.title,
            "controls": [c.to_dict() for c in self.controls],
        }


@dataclass
class Page:
    """One screen of the form."""

    id: str | None
    title: str
    sections: list[Section] = dc_field(default_factory=list)

    @property
    def field_count(self) -> int:
        return sum(len(s.controls) for s in self.sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "fields": self.field_count,
            "sections": [s.to_dict() for s in self.sections],
        }


@dataclass
class Layout:
    """The whole form, ready to be drawn."""

    name: str
    pages: list[Page] = dc_field(default_factory=list)

    @property
    def field_count(self) -> int:
        return sum(p.field_count for p in self.pages)

    def names(self) -> list[str]:
        return [c.name for p in self.pages for s in p.sections for c in s.controls]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fields": self.field_count,
            "pages": [p.to_dict() for p in self.pages],
        }


def _control_for(field: Field) -> Control:
    control = (field.control or "text").lower()
    input_type = "text"
    if control in ("select", "multiselect", "radio", "checkbox", "textarea"):
        pass
    elif control in NATIVE_INPUTS:
        input_type, control = control, "input"
    else:
        # An input type this build has never heard of. A browser falls back to
        # a text box for exactly the same reason, so do the same rather than
        # dropping the field.
        control = "input"

    # A field with options but a control that cannot show them - a spec that
    # named choices without naming a control - is a dropdown in all but name.
    if field.options and control == "input":
        control = "select"

    constraints = field.constraints
    return Control(
        name=field.name,
        label=field.label or field.name,
        control=control,
        input_type=input_type,
        options=[o.to_dict() for o in field.options],
        placeholder=field.placeholder,
        help_text=field.help_text,
        required=constraints.required,
        max_length=constraints.max_length,
        minimum=constraints.minimum,
        maximum=constraints.maximum,
        step=constraints.step,
        pattern=constraints.pattern,
        group=field.group,
        semantic_type=field.semantic_type,
    )


def _title_for(group: str | None) -> str:
    if not group:
        return "Details"
    return group.replace("_", " ").replace("-", " ").strip().title()


def layout(schema: FormSchema) -> Layout:
    """Lay the schema out as pages of grouped controls, in schema order.

    Field order is the source's own, which is the order the form was designed
    to be filled in. Groups are contiguous runs rather than a bucket per
    name, so a form that returns to an earlier group - a second address at
    the end - is drawn the way it was written rather than reordered into
    tidiness that would not match the real screen.
    """
    titles = {s.id: s.title for s in schema.screens}
    pages: list[Page] = []

    for field in schema.fields:
        if not _is_fillable(field):
            continue
        screen = field.screen
        if not pages or pages[-1].id != screen:
            pages.append(Page(id=screen, title=titles.get(screen or "") or screen
                              or schema.name or "Form"))
        page = pages[-1]
        if not page.sections or page.sections[-1].group != field.group:
            page.sections.append(Section(group=field.group,
                                         title=_title_for(field.group)))
        page.sections[-1].controls.append(_control_for(field))

    if not pages:
        pages.append(Page(id=None, title=schema.name or "Form"))
    return Layout(name=schema.name or "form", pages=pages)
