"""One turn of a conversation, as a pure function of what it is given.

Typing, speaking and clicking a suggested action all arrive here the same
way - as one ``input`` - and every reply has the same shape. That is the whole
design: a chat window built against this cannot end up with three code paths
that disagree about what "Submit" means. See ``docs/bot-builder.md``.

Nothing is kept between calls. The conversation travels in ``state``, which
the client sends back unchanged, and which is checked again on every turn
because it has passed through somebody's browser on the way.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from . import understand
from .template import Template, TemplateField
from .understand import Found, Problem, Reading

STATE_VERSION = 1

STATUSES = ("idle", "collecting", "ready", "submitting", "handed_off", "done", "cancelled")
#: A conversation in one of these has finished; the next phrase starts anew.
FINISHED = ("handed_off", "done", "cancelled")
SOURCES = ("said", "clicked", "model")
EVENTS = ("submitted", "submit_failed", "filled")

#: A field with this many options or fewer is asked as buttons.
ANSWER_BUTTONS = 6
#: How many templates are offered when nobody can tell which one is meant.
CHOICES = 4
MAX_TEXT = 1000
MAX_ALTERNATIVES = 5
#: A different template has to beat the one in progress by this much before
#: a phrase counts as changing the subject.
SWITCH_MARGIN = 0.2
SWITCH_AT = 0.5

LABELS = {
    "submit": "Submit",
    "fill_form": "Fill the form for manual submission",
    "cancel": "Start over",
}

#: ``reader(templates, active, text, expects=..., last_field=...) -> Reading``
Reader = Callable[..., Reading]
#: ``complete(template, observed) -> {field: (value, confidence)}``
Completer = Callable[[Template, dict[str, str]], dict[str, tuple[str, float]]]


class BotError(Exception):
    """A turn that cannot be taken, with a stable code for the caller."""

    def __init__(self, message: str, code: str = "bad_input", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ----------------------------------------------------------------------
# the state that travels with the conversation
# ----------------------------------------------------------------------


@dataclass
class Value:
    value: str
    source: str = "said"
    confidence: float = 0.9
    said_before: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "source": self.source,
                               "confidence": round(self.confidence, 3)}
        if self.said_before:
            out["said_before"] = self.said_before
        return out


@dataclass
class State:
    id: str
    turn: int = 0
    status: str = "idle"
    template: str | None = None
    values: dict[str, Value] | None = None
    expects: str | None = None
    last_field: str | None = None
    offered: list[str] | None = None
    #: What was said before the person had to pick a template, read again
    #: once they have.
    pending: str = ""
    #: Templates set aside by a change of subject, with what they had.
    parked: dict[str, dict[str, Value]] | None = None

    def __post_init__(self) -> None:
        self.values = self.values or {}
        self.offered = self.offered or []
        self.parked = self.parked or {}

    @classmethod
    def fresh(cls) -> "State":
        return cls(id="cnv-" + uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": STATE_VERSION,
            "id": self.id,
            "turn": self.turn,
            "status": self.status,
            "template": self.template,
            "values": {k: v.to_dict() for k, v in self.values.items()},
            "expects": self.expects,
            "last_field": self.last_field,
            "offered": list(self.offered),
            "pending": self.pending,
            "parked": {key: {k: v.to_dict() for k, v in values.items()}
                       for key, values in self.parked.items()},
        }

    def reset(self) -> None:
        self.template = None
        self.values = {}
        self.expects = None
        self.last_field = None
        self.pending = ""


def _bad(message: str) -> BotError:
    return BotError(f"the conversation state is not usable: {message}",
                    code="bad_state")


def _values(raw: Any, template: Template) -> dict[str, Value]:
    if not isinstance(raw, dict):
        raise _bad("values must be an object")
    out: dict[str, Value] = {}
    for name, item in raw.items():
        field = template.field(str(name))
        if field is None:
            raise _bad(f"{template.key} has no field called {name!r}")
        if not isinstance(item, dict):
            raise _bad(f"the value for {name} must be an object")
        source = str(item.get("source") or "said")
        if source not in SOURCES:
            raise _bad(f"{source!r} is not where a value can come from")
        value, reason = understand.accept(field, str(item.get("value") or ""))
        if value is None:
            raise _bad(f"{name}: {reason}")
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.9))))
        except (TypeError, ValueError):
            raise _bad(f"the confidence for {name} is not a number") from None
        out[field.name] = Value(value=value, source=source, confidence=confidence,
                                said_before=str(item.get("said_before") or "")[:200])
    return out


def read_state(raw: Any, templates: dict[str, Template]) -> State:
    """A state sent back by a client, re-checked from scratch."""
    if raw is None:
        return State.fresh()
    if not isinstance(raw, dict):
        raise _bad("it must be the object the last reply gave")
    if raw.get("v") != STATE_VERSION:
        raise _bad(f"this service reads version {STATE_VERSION}")
    conversation = str(raw.get("id") or "")
    if not conversation.startswith("cnv-") or len(conversation) > 40:
        raise _bad("it has no conversation id")
    status = str(raw.get("status") or "idle")
    if status not in STATUSES:
        raise _bad(f"{status!r} is not a status")
    try:
        turn = int(raw.get("turn") or 0)
    except (TypeError, ValueError):
        raise _bad("the turn is not a number") from None

    state = State(id=conversation, turn=turn, status=status)
    key = raw.get("template")
    if key is not None:
        template = templates.get(str(key))
        if template is None:
            raise BotError(f"the template {key!r} is not available to this chat",
                           code="template_not_allowed", status=403)
        state.template = template.key
        state.values = _values(raw.get("values") or {}, template)
        for name in ("expects", "last_field"):
            given = raw.get(name)
            if given is not None and template.field(str(given)) is None:
                raise _bad(f"{template.key} has no field called {given!r}")
            setattr(state, name, str(given) if given is not None else None)
    offered = raw.get("offered") or []
    if not isinstance(offered, list) or len(offered) > 40:
        raise _bad("offered must be a short list")
    state.offered = [str(a)[:120] for a in offered]
    state.pending = str(raw.get("pending") or "")[:MAX_TEXT]
    parked = raw.get("parked") or {}
    if not isinstance(parked, dict):
        raise _bad("parked must be an object")
    for pkey, pvalues in parked.items():
        if pkey in templates:
            state.parked[pkey] = _values(pvalues, templates[pkey])
    return state


# ----------------------------------------------------------------------
# the input: one of three shapes
# ----------------------------------------------------------------------


@dataclass
class Input:
    type: str
    text: str = ""
    via: str = "typed"
    alternatives: list[str] | None = None
    action: str = ""
    event: str = ""
    detail: dict[str, Any] | None = None


def read_input(raw: Any) -> Input:
    if not isinstance(raw, dict):
        raise BotError("input must be an object with a type")
    kind = raw.get("type")
    if kind == "text":
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            raise BotError("a text input needs some text")
        if len(text) > MAX_TEXT:
            raise BotError(f"that is more than {MAX_TEXT} characters", code="too_large",
                           status=413)
        via = raw.get("via") or "typed"
        if via not in ("typed", "speech"):
            raise BotError("via must be typed or speech")
        alternatives = raw.get("alternatives") or []
        if not isinstance(alternatives, list):
            raise BotError("alternatives must be a list of strings")
        alternatives = [" ".join(str(a).split())[:MAX_TEXT] for a in alternatives
                        if str(a).strip()][:MAX_ALTERNATIVES]
        return Input("text", text=text, via=via, alternatives=alternatives)
    if kind == "action":
        action = str(raw.get("action") or "")
        if not action:
            raise BotError("an action input needs the action's id")
        return Input("action", action=action)
    if kind == "event":
        event = str(raw.get("event") or "")
        if event not in EVENTS:
            raise BotError("event must be one of " + ", ".join(EVENTS))
        detail = raw.get("detail") or {}
        if not isinstance(detail, dict):
            raise BotError("detail must be an object")
        return Input("event", event=event, detail=detail)
    raise BotError("input.type must be text, action or event")


# ----------------------------------------------------------------------
# the turn
# ----------------------------------------------------------------------


class Turn:
    """Everything one turn builds up before it becomes a reply."""

    def __init__(self, templates: list[Template], state: State,
                 current: dict[str, str], via: str) -> None:
        self.templates = templates
        self.by_key = {t.key: t for t in templates}
        self.state = state
        self.current = current
        self.via = via
        self.messages: list[str] = []
        self.effect: dict[str, Any] | None = None
        self.choices: list[str] = []
        self.intent: dict[str, Any] | None = None
        self.changed_now: list[str] = []
        self.problems: list[Problem] = []

    @property
    def template(self) -> Template | None:
        return self.by_key.get(self.state.template or "")

    def say(self, text: str) -> None:
        if text:
            self.messages.append(text)

    # -- what a finished form looks like --------------------------------

    def form(self) -> dict[str, Any] | None:
        template = self.template
        if template is None:
            return None
        rows, values, changes, missing = [], {}, {}, []
        for f in template.fields:
            before = str(self.current.get(f.name, "") or "")
            held = self.state.values.get(f.name)
            row: dict[str, Any] = {"name": f.name, "label": f.label,
                                   "required": f.required, "before": before or None}
            if held is not None:
                row.update(after=held.value, source=held.source,
                           confidence=round(held.confidence, 3),
                           status="changed" if held.value != before else "unchanged")
                if held.said_before:
                    row["said_before"] = held.said_before
                values[f.name] = held.value
                if held.value != before:
                    changes[f.name] = {"before": before or None, "after": held.value}
            elif before and any(src in self.state.values
                                and self.state.values[src].value != self.current.get(src, "")
                                for src in f.follows):
                # What is on file was true of the old city, not the new one.
                row.update(after=None, status="outdated")
                if f.required:
                    missing.append(f.name)
            elif before:
                row.update(after=before, source="current", status="kept")
                values[f.name] = before
            else:
                row.update(after=None, status="missing" if f.required else "empty")
                if f.required:
                    missing.append(f.name)
            rows.append(row)
        return {"template": template.key, "fields": rows, "values": values,
                "changes": changes, "missing": missing, "complete": not missing}

    def missing(self) -> list[TemplateField]:
        form = self.form()
        template = self.template
        if form is None or template is None:
            return []
        return [template.field(n) for n in form["missing"]]  # type: ignore[misc]


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def take_turn(templates: list[Template], request: dict[str, Any], *,
              reader: Reader | None = None,
              complete: Completer | None = None) -> dict[str, Any]:
    """One turn: read the input, move the conversation on, say what next.

    ``templates`` are the ones this chat may use, already narrowed by the
    caller to what the token and ``context.templates`` allow. ``reader`` is
    the local reading unless the service was started with a language model
    turned on; ``complete`` is the linked autofill model, if any.
    """
    if not templates:
        raise BotError("there are no templates to talk about yet; create one first",
                       code="no_templates", status=409)
    if not isinstance(request, dict):
        raise BotError("the request must be an object")
    context = request.get("context") or {}
    if not isinstance(context, dict):
        raise BotError("context must be an object")
    current_raw = context.get("current") or {}
    if not isinstance(current_raw, dict):
        raise BotError("context.current must be an object of field names to values")
    current = {str(k): " ".join(str(v).split())[:500] for k, v in current_raw.items()
               if v is not None and not isinstance(v, (dict, list))}

    by_key = {t.key: t for t in templates}
    state = read_state(request.get("state"), by_key)
    given = read_input(request.get("input"))
    turn = Turn(templates, state, current, given.via)
    reader = reader or understand.read

    state.turn += 1
    finishing = state.status in FINISHED
    if finishing and given.type != "event":
        # A finished conversation that is spoken to again starts a new one
        # under the same id: the chat window is still the same chat.
        state.reset()
        state.status = "idle"

    start = context.get("template")
    if start and state.template is None and given.type == "text":
        if start not in by_key:
            raise BotError(f"the template {start!r} is not available to this chat",
                           code="template_not_allowed", status=403)
        _begin(turn, by_key[start], how="context", confidence=1.0)

    if given.type == "event":
        _on_event(turn, given)
    elif given.type == "action":
        if given.action not in state.offered:
            raise BotError(
                "that action was not offered on the last turn; it may already "
                "have been used", code="stale_action", status=409)
        _on_action(turn, given.action, reader, complete)
    else:
        phrase = understand.action_phrase(given.text)
        action = _phrase_action(turn, phrase)
        if action is not None:
            _on_action(turn, action, reader, complete)
        elif phrase in ("submit", "fill_form") and turn.template is not None:
            # Asked for something that is not on offer: say why, rather than
            # reading "submit" as a value for whatever was asked last.
            _intent_continued(turn)
            if phrase == "submit" and "submit" not in turn.template.actions:
                turn.say("This one can't be submitted from the chat.")
            elif phrase == "fill_form" and "fill_form" not in turn.template.actions:
                turn.say("This one is submitted from the chat rather than the form.")
            elif turn.missing():
                needed = [_word(f.label) for f in turn.missing()]
                turn.say(f"I still need the {_join(needed)} before I can submit it.")
            _next_step(turn)
        else:
            _on_text(turn, given, reader, complete)

    return _reply(turn, given, complete)


# -- the three kinds of input ------------------------------------------


def _phrase_action(turn: Turn, phrase: str | None) -> str | None:
    """The offered action a phrase means, if it means one."""
    offered = turn.state.offered
    if phrase is None:
        return None
    if phrase == "confirm":
        for candidate in ("submit", "fill_form"):
            if candidate in offered:
                return candidate
        return None
    return phrase if phrase in offered else None


def _begin(turn: Turn, template: Template, *, how: str, confidence: float) -> None:
    state = turn.state
    previous = turn.template
    if previous is not None and previous.key != template.key and state.values:
        state.parked[previous.key] = dict(state.values)
        turn.say(f"I've set the {previous.name.lower()} aside.")
    changed = previous is None or previous.key != template.key
    state.template = template.key
    if changed:
        state.values = state.parked.pop(template.key, {}) or {}
        state.expects = None
        state.last_field = None
    state.status = "collecting"
    turn.intent = {"template": template.key, "name": template.name,
                   "confidence": round(confidence, 3), "how": how, "changed": changed}


def _on_text(turn: Turn, given: Input, reader: Reader, complete: Completer | None) -> None:
    state = turn.state
    active = turn.template
    texts = [given.text] + list(given.alternatives or [])

    reading = None
    for text in texts:
        reading = reader(turn.templates, active, text, expects=state.expects,
                         last_field=state.last_field)
        if reading.values or reading.template or understand.decide(reading.scores)[0]:
            break
    assert reading is not None
    how = reading.how

    if active is None:
        key = reading.template
        confidence = dict(reading.scores).get(key, 0.0) if key else 0.0
        if key is None:
            key, choices = understand.decide(reading.scores)
            confidence = reading.best()[1]
            if key is None:
                state.pending = given.text
                _ask_which(turn, choices)
                return
        _begin(turn, turn.by_key[key], how=how, confidence=max(confidence, 0.0))
        if not reading.values and active is None and reading.template is None:
            # The reading was of the best template already, so nothing more
            # to read - but a model reader may not have read values yet.
            pass
    else:
        # A clear change of subject is a switch; anything less is more
        # information for the template already in progress.
        best, best_score = reading.template, 0.0
        if best is None:
            best, best_score = reading.best()
        mine = dict(reading.scores).get(active.key, 0.0)
        named = reading.template is not None and reading.template != active.key
        switching = best is not None and best != active.key and (
            named or (not reading.values and best_score >= SWITCH_AT
                      and best_score >= mine + SWITCH_MARGIN))
        if switching:
            _begin(turn, turn.by_key[best], how=how, confidence=best_score or 0.9)
            if not named or not reading.values:
                # The values read were for the template being left.
                reading = reader(turn.templates, turn.template, given.text)
        else:
            turn.intent = {"template": active.key, "name": active.name,
                           "confidence": round(max(mine, 0.0), 3), "how": "continued",
                           "changed": False}

    _merge(turn, reading.values, "said")
    turn.problems = reading.problems
    if not reading.values and not reading.problems and turn.intent and \
            not turn.intent.get("changed"):
        turn.say("Sorry, I didn't catch a value in that.")
    _complete(turn, complete)
    _next_step(turn)


def _on_action(turn: Turn, action: str, reader: Reader, complete: Completer | None) -> None:
    state = turn.state
    template = turn.template

    if action.startswith("choose:"):
        key = action.split(":", 1)[1]
        if key not in turn.by_key:
            raise BotError(f"no template called {key!r}", code="unknown_template", status=404)
        _begin(turn, turn.by_key[key], how="chosen", confidence=1.0)
        pending, state.pending = state.pending, ""
        if pending:
            values, problems = understand.read_values(turn.by_key[key], pending)
            _merge(turn, values, "said")
            turn.problems = problems
        _complete(turn, complete)
        _next_step(turn)
        return

    if action.startswith("answer:"):
        _, name, index = (action.split(":", 2) + ["", ""])[:3]
        field = template.field(name) if template else None
        try:
            option = field.options[int(index)] if field else None
        except (ValueError, IndexError):
            option = None
        if field is None or option is None:
            raise BotError("that answer does not belong to this form", code="stale_action",
                           status=409)
        _merge(turn, {field.name: Found(option, how="clicked", confidence=1.0)}, "clicked")
        _intent_continued(turn)
        _complete(turn, complete)
        _next_step(turn)
        return

    if action == "cancel":
        name = template.name.lower() if template else "that"
        state.reset()
        state.parked = {}
        state.status = "cancelled"
        turn.say(f"OK, I've cleared the {name}. What would you like to do?")
        turn.choices = [t.key for t in turn.templates][:CHOICES]
        return

    if template is None:
        raise BotError("there is no form in progress", code="stale_action", status=409)
    _intent_continued(turn)
    form = turn.form() or {}

    if action == "submit":
        if form.get("missing"):
            _next_step(turn)
            return
        state.status = "submitting"
        state.expects = None
        turn.effect = {"type": "submit", "template": template.key,
                       "values": form["values"], "changes": form["changes"]}
        turn.say(f"Submitting your {template.name.lower()}.")
        return

    if action == "fill_form":
        state.status = "handed_off"
        state.expects = None
        turn.effect = {"type": "fill_form", "template": template.key,
                       "values": form["values"], "changes": form["changes"]}
        left = [template.field(n).label for n in form.get("missing", [])]  # type: ignore[union-attr]
        tail = f" It still needs {_join([_word(l) for l in left])}." if left else ""
        turn.say("I've put this into the form for you to check and submit." + tail)
        return

    raise BotError(f"{action!r} is not an action this service knows", code="stale_action",
                   status=409)


def _on_event(turn: Turn, given: Input) -> None:
    state = turn.state
    template = turn.template
    detail = given.detail or {}
    if given.event == "submitted":
        if state.status != "submitting":
            raise BotError("nothing was being submitted", code="stale_action", status=409)
        state.status = "done"
        state.expects = None
        reference = str(detail.get("reference") or "")[:80]
        message = (template.done_message if template and template.done_message
                   else "Done, that's submitted.")
        turn.say(message + (f" Your reference is {reference}." if reference else ""))
    elif given.event == "submit_failed":
        if state.status != "submitting":
            raise BotError("nothing was being submitted", code="stale_action", status=409)
        state.status = "ready"
        why = str(detail.get("message") or "")[:200]
        turn.say("That didn't go through" + (f": {why}" if why else "") +
                 ". You can try again, or fill the form in yourself.")
    else:
        if state.status not in ("handed_off", "done"):
            state.status = "handed_off"
    if template is not None:
        turn.intent = {"template": template.key, "name": template.name,
                       "confidence": 1.0, "how": "continued", "changed": False}


def _intent_continued(turn: Turn) -> None:
    template = turn.template
    if template is not None and turn.intent is None:
        turn.intent = {"template": template.key, "name": template.name,
                       "confidence": 1.0, "how": "continued", "changed": False}


# -- moving the conversation on ----------------------------------------


def _merge(turn: Turn, found: dict[str, Found], source: str) -> None:
    template = turn.template
    if template is None:
        return
    for name, item in found.items():
        field = template.field(name)
        if field is None:
            continue
        value, reason = understand.accept(field, item.value)
        if value is None:
            turn.problems.append(Problem(name, item.value, reason))
            continue
        turn.state.values[name] = Value(value=value, source=source,
                                        confidence=item.confidence,
                                        said_before=item.said_before)
        turn.state.last_field = name
        turn.changed_now.append(name)


def _complete(turn: Turn, complete: Completer | None) -> None:
    """Ask the linked model about the fields nobody mentioned."""
    template = turn.template
    if complete is None or template is None or not turn.changed_now:
        return
    observed = {k: v.value for k, v in turn.state.values.items()
                if v.source in ("said", "clicked")}
    try:
        guesses = complete(template, observed)
    except Exception:  # noqa: BLE001 - a model that fails costs its guesses, not the turn
        return
    for name, (value, confidence) in guesses.items():
        field = template.field(name)
        held = turn.state.values.get(name)
        if field is None or (held is not None and held.source != "model"):
            continue
        accepted, _ = understand.accept(field, value)
        if accepted is not None:
            turn.state.values[name] = Value(accepted, source="model",
                                            confidence=confidence)


def _next_step(turn: Turn) -> None:
    template = turn.template
    state = turn.state
    if template is None:
        return
    if turn.changed_now:
        parts = [f"{_word(template.field(n).label)} to {state.values[n].value}"  # type: ignore[union-attr]
                 for n in dict.fromkeys(turn.changed_now)]
        lead = f"OK, {_a(template.name)}. " if turn.intent and \
            turn.intent.get("changed") else ""
        turn.say(lead + _sentence(_join(parts)) + ".")
    elif turn.intent and turn.intent.get("changed"):
        turn.say(f"Sure, {_a(template.name)}.")
    for problem in turn.problems[:2]:
        label = template.field(problem.field).label if template.field(problem.field) else problem.field
        turn.say(f"I couldn't use “{problem.said}” for {_word(label)}: {problem.reason}.")

    missing = turn.missing()
    if missing:
        ask = missing[0]
        state.expects = ask.name
        state.status = "collecting"
        hint = f" For example {ask.example}." if ask.example and turn.via == "typed" else ""
        turn.say(f"What is the {'new ' if _is_change(template) else ''}{_word(ask.label)}?{hint}")
    else:
        state.expects = None
        state.status = "ready"
        can = [LABELS[a].lower() for a in ("submit", "fill_form") if a in template.actions]
        if "fill_form" in template.actions and "submit" in template.actions:
            turn.say("That's everything. Shall I submit it, or fill the form so you can check it first?")
        elif "submit" in template.actions:
            turn.say("That's everything. Shall I submit it?")
        else:
            turn.say(f"That's everything. I can {can[0]}.")


def _word(label: str) -> str:
    """A label as it reads mid-sentence: "Street address" -> "street address",
    but "ZIP code" stays as it is."""
    first = label.split(" ", 1)[0]
    return label if first.isupper() and len(first) > 1 else label[:1].lower() + label[1:]


def _a(name: str) -> str:
    name = name.lower()
    return ("an " if name[:1] in "aeiou" else "a ") + name


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _is_change(template: Template) -> bool:
    words = understand.content_words(template.name + " " + template.description)
    return bool(words & {"chang", "change", "updat", "update", "new", "mov", "move"})


# -- the reply ----------------------------------------------------------


def _actions(turn: Turn) -> list[dict[str, Any]]:
    template = turn.template
    state = turn.state
    out: list[dict[str, Any]] = []
    for key in turn.choices:
        t = turn.by_key[key]
        out.append({"id": f"choose:{key}", "type": "choose", "label": t.name,
                    "template": key})
    if template is None or state.status in ("submitting", "handed_off", "done", "cancelled"):
        return out

    form = turn.form() or {}
    if state.expects:
        field = template.field(state.expects)
        if field is not None and field.options and len(field.options) <= ANSWER_BUTTONS:
            for index, option in enumerate(field.options):
                out.append({"id": f"answer:{field.name}:{index}", "type": "answer",
                            "label": option, "field": field.name, "value": option})
    if "submit" in template.actions and not form.get("missing") and form.get("values"):
        out.append({"id": "submit", "type": "submit", "label": LABELS["submit"]})
    if "fill_form" in template.actions and (state.values or form.get("changes")):
        out.append({"id": "fill_form", "type": "fill_form", "label": LABELS["fill_form"]})
    out.append({"id": "cancel", "type": "cancel", "label": LABELS["cancel"]})
    for kind in ("submit", "fill_form"):
        chosen = next((a for a in out if a["type"] == kind), None)
        if chosen is not None:
            chosen["style"] = "primary"
            break
    return out


def _ask_which(turn: Turn, choices: list[str]) -> None:
    turn.state.status = "idle"
    if choices:
        names = [turn.by_key[k].name.lower() for k in choices]
        turn.say(f"Do you mean {' or '.join(names)}?")
        turn.choices = choices
    else:
        names = [t.name.lower() for t in turn.templates[:CHOICES]]
        turn.say(f"I can help with {_join(names)}. Which would you like?")
        turn.choices = [t.key for t in turn.templates][:CHOICES]
    turn.intent = {"template": None, "name": None, "confidence": 0.0,
                   "how": "unclear", "changed": False}


def _reply(turn: Turn, given: Input, complete: Completer | None) -> dict[str, Any]:
    state = turn.state
    actions = _actions(turn)
    state.offered = [a["id"] for a in actions]
    expects = None
    template = turn.template
    if state.expects and template is not None:
        field = template.field(state.expects)
        if field is not None:
            expects = {"field": field.name, "label": field.label,
                       "semantic_type": field.semantic_type}
    reply: dict[str, Any] = {
        "conversation": {"id": state.id, "turn": state.turn, "status": state.status},
        "intent": turn.intent or ({"template": template.key, "name": template.name,
                                   "confidence": 1.0, "how": "continued", "changed": False}
                                  if template else None),
        "messages": [{"role": "bot", "text": " ".join(turn.messages)}] if turn.messages else [],
        "form": turn.form(),
        "actions": actions,
        "expects": expects,
        "effect": turn.effect,
        "state": state.to_dict(),
    }
    if given.type == "text":
        reply["understood"] = {"text": given.text, "via": given.via}
    return reply
