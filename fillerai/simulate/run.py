"""The run loop: an agent types a few fields, the model finishes the form.

This is the phase that puts the other three together and asks the only
question that decides whether any of it was worth building - *how much of
this form does someone still have to fill in themselves?* Everything here
exists to answer that honestly, which means three numbers rather than one:

* **What got filled.** Predictions the model was confident enough to offer,
  and the fields it declined, which are a result and not a gap.
* **Whether it was right.** Only answerable against a form whose true
  contents are known, so a run can carry a *case*: a record the model has
  never seen, standing in for the customer in front of the agent.
* **What that saved.** Measured against typing the same form out by hand,
  under the assumptions in :mod:`.effort` - with the review of every filled
  value, and the cost of correcting the wrong ones, charged against the
  saving rather than quietly left out.

A run is a pure function of the model, what has been typed, and the case.
Nothing is remembered between calls, so the UI can ask for a fresh board on
every keystroke and the CLI can ask for one per record, and they cannot
disagree about what the model thinks.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from ..schema import FormSchema
from ..train import features
from ..train.model import ACCEPT_ABOVE, AutofillModel
from .effort import DEFAULT_EFFORT, Effort, spell_out
from .form import layout

# Where a value on the board came from.
TYPED = "typed"          # the agent entered it
FILLED = "filled"        # the model was confident enough to fill it in
SUGGESTED = "suggested"  # the model has an answer, below the threshold to use it
YOURS = "yours"          # the model declines; this one is the agent's to type

# Length assumed for a field nothing is known about. Roughly a short code or
# a city name - long enough not to flatter the saving, short enough not to
# invent one.
ASSUMED_LENGTH = 10


def _share_words(share: float) -> str:
    """The saving in words, including when there is not one.

    A model that fills boxes wrongly costs an agent more than an empty form
    does, and the report that says so as "-3% less" is asking the reader to
    do the work of noticing. It says "3% more" instead.
    """
    if share < 0:
        return f"{-share * 100:.0f}% more"
    return f"{share * 100:.0f}% less"


@dataclass
class Cell:
    """One field of the form, as the simulated agent would find it."""

    name: str
    source: str = YOURS
    value: str = ""
    confidence: float = 0.0
    because: list[str] = dc_field(default_factory=list)
    alternatives: list[tuple[str, float]] = dc_field(default_factory=list)
    format: str | None = None
    truth: str | None = None
    # "right" / "wrong" against the case, or None when there is nothing to
    # check: no case loaded, the case left the field blank, or the agent
    # typed it themselves.
    verdict: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "source": self.source,
            "value": self.value,
            "confidence": round(self.confidence, 4),
        }
        if self.because:
            out["because"] = self.because
        if self.alternatives:
            out["alternatives"] = [[v, round(p, 4)] for v, p in self.alternatives]
        if self.format:
            out["format"] = self.format
        if self.truth is not None:
            out["truth"] = self.truth
        if self.verdict:
            out["verdict"] = self.verdict
        return out


@dataclass
class Savings:
    """What the fill cost, against typing the same form out."""

    fields: int = 0
    typed: int = 0
    filled: int = 0
    suggested: int = 0
    left: int = 0
    wrong: int = 0
    keystrokes_by_hand: int = 0
    keystrokes_now: int = 0
    seconds_by_hand: float = 0.0
    seconds_now: float = 0.0

    @property
    def keystrokes_saved(self) -> int:
        return self.keystrokes_by_hand - self.keystrokes_now

    @property
    def seconds_saved(self) -> float:
        return self.seconds_by_hand - self.seconds_now

    @property
    def share_saved(self) -> float:
        if self.seconds_by_hand <= 0:
            return 0.0
        return self.seconds_saved / self.seconds_by_hand

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": self.fields,
            "typed": self.typed,
            "filled": self.filled,
            "suggested": self.suggested,
            "left": self.left,
            "wrong": self.wrong,
            "keystrokes_by_hand": self.keystrokes_by_hand,
            "keystrokes_now": self.keystrokes_now,
            "keystrokes_saved": self.keystrokes_saved,
            "seconds_by_hand": round(self.seconds_by_hand, 1),
            "seconds_now": round(self.seconds_now, 1),
            "seconds_saved": round(self.seconds_saved, 1),
            "share_saved": round(self.share_saved, 4),
            "by_hand": spell_out(self.seconds_by_hand),
            "now": spell_out(self.seconds_now),
            "saved": spell_out(self.seconds_saved),
        }


@dataclass
class Score:
    """How the filled values compared with the case, where one was loaded."""

    checked: int = 0  # filled values with a truth to compare against
    right: int = 0
    wrong: int = 0
    # Below the threshold, so not filled in. Counted separately because it
    # says what moving the threshold would buy or cost.
    held_back: int = 0
    held_back_right: int = 0
    # Fields the model declined and the case had a value for: the work the
    # agent is genuinely left with, as opposed to boxes that stay empty.
    declined: int = 0

    @property
    def accuracy(self) -> float:
        return self.right / self.checked if self.checked else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "right": self.right,
            "wrong": self.wrong,
            "accuracy": round(self.accuracy, 4),
            "held_back": self.held_back,
            "held_back_right": self.held_back_right,
            "declined": self.declined,
        }


@dataclass
class Run:
    """One pass over the form: the board, the saving, and the score."""

    cells: list[Cell] = dc_field(default_factory=list)
    savings: Savings = dc_field(default_factory=Savings)
    score: Score | None = None
    threshold: float = ACCEPT_ABOVE

    def record(self) -> dict[str, str]:
        """The form as it would be submitted - typed and filled values only."""
        return {c.name: c.value for c in self.cells
                if c.source in (TYPED, FILLED) and c.value}

    def headline(self) -> str:
        saving = self.savings
        line = (
            f"{saving.typed} typed, {saving.filled} filled, {saving.left} left "
            f"to type: {spell_out(saving.seconds_now)} against "
            f"{spell_out(saving.seconds_by_hand)} by hand, "
            f"{_share_words(saving.share_saved)}"
        )
        if self.score and self.score.checked:
            line += (f"; {self.score.right} of {self.score.checked} filled values "
                     f"matched the case")
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "cells": [c.to_dict() for c in self.cells],
            "savings": self.savings.to_dict(),
            "score": self.score.to_dict() if self.score else None,
            "record": self.record(),
            "headline": self.headline(),
        }


# ----------------------------------------------------------------------
# one run
# ----------------------------------------------------------------------


def _typical_length(model: AutofillModel, name: str) -> int:
    """How long this field's values usually are, for costing a box by hand.

    With a case loaded every length is the real one and this is not used.
    Without one, the alternative to guessing is to report no saving at all
    for the fields the model declines, which would overstate the saving
    rather than understate it.
    """
    profile = model.profiles.get(name)
    if profile is not None:
        lengths = [len(v) for v, _ in profile.values if v]
        if lengths:
            return round(sum(lengths) / len(lengths))
        if profile.format:
            return len(profile.format)
    return ASSUMED_LENGTH


def _controls(model: AutofillModel) -> dict[str, str]:
    """Each field's control kind, which is what decides its cost to fill."""
    return {c.name: c.control if c.control != "input" else c.input_type
            for page in layout(model.schema).pages
            for section in page.sections
            for c in section.controls}


def run(model: AutofillModel, typed: dict[str, Any] | None = None, *,
        case: dict[str, Any] | None = None,
        threshold: float = ACCEPT_ABOVE,
        effort: Effort = DEFAULT_EFFORT) -> Run:
    """Fill the form from ``typed`` and cost the result.

    ``case`` is the record the form is really about, when one is known: it
    supplies the truth each filled value is checked against, and the real
    lengths of the boxes the agent is still left to type.
    """
    typed = {k: features.normalise(v) for k, v in (typed or {}).items()}
    typed = {k: v for k, v in typed.items() if v and k in model.profiles}
    truths = {k: features.normalise(v) for k, v in (case or {}).items()} if case else {}

    predictions = model.predict(typed)
    controls = _controls(model)
    result = Run(threshold=threshold)
    score = Score() if case else None
    savings = Savings()

    for name in model.targets():
        control = controls.get(name, "text")
        truth = truths.get(name) if case else None
        cell = Cell(name=name, truth=truth if case else None)

        if name in typed:
            cell.source, cell.value = TYPED, typed[name]
            _charge_typed(savings, effort, control, len(cell.value))
            _charge_by_hand(savings, effort, control, len(cell.value))
            savings.typed += 1
            savings.fields += 1
            result.cells.append(cell)
            continue

        prediction = predictions.get(name)
        length = len(truth) if truth else _typical_length(model, name)

        if prediction is None or not prediction.known:
            cell.source = YOURS
            cell.because = list(prediction.because) if prediction else []
            cell.format = prediction.format if prediction else None
            savings.left += 1
            if score is not None and truth:
                score.declined += 1
            _charge_typed(savings, effort, control, length)
        else:
            cell.confidence = prediction.confidence
            cell.because = list(prediction.because)
            cell.alternatives = list(prediction.alternatives)
            cell.format = prediction.format
            hit = bool(truth) and prediction.value == truth
            if truth:
                cell.verdict = "right" if hit else "wrong"

            if prediction.confidence >= threshold:
                cell.source, cell.value = FILLED, prediction.value or ""
                savings.filled += 1
                if score is not None and truth:
                    score.checked += 1
                    score.right += int(hit)
                    score.wrong += int(not hit)
                if truth and not hit:
                    # A wrong fill is worse than an empty box: the agent has
                    # to notice it before retyping it. Charging that to the
                    # fill is the only way the saving stays honest.
                    savings.wrong += 1
                    _charge_correct(savings, effort, control, length)
                else:
                    _charge_review(savings, effort)
            else:
                # Shown, greyed, but not put in the box. The agent still
                # types it, and the suggestion beside the box is worth
                # something, but not enough to claim as a saving.
                cell.source, cell.value = SUGGESTED, prediction.value or ""
                savings.suggested += 1
                savings.left += 1
                if score is not None and truth:
                    score.held_back += 1
                    score.held_back_right += int(hit)
                _charge_typed(savings, effort, control, length)

        _charge_by_hand(savings, effort, control, length)
        savings.fields += 1
        result.cells.append(cell)

    result.savings = savings
    result.score = score
    return result


def _charge_typed(savings: Savings, effort: Effort, control: str, length: int) -> None:
    keystrokes, seconds = effort.type_cost(control, length)
    savings.keystrokes_now += keystrokes
    savings.seconds_now += seconds


def _charge_review(savings: Savings, effort: Effort) -> None:
    keystrokes, seconds = effort.review_cost()
    savings.keystrokes_now += keystrokes
    savings.seconds_now += seconds


def _charge_correct(savings: Savings, effort: Effort, control: str, length: int) -> None:
    keystrokes, seconds = effort.correct_cost(control, length)
    savings.keystrokes_now += keystrokes
    savings.seconds_now += seconds


def _charge_by_hand(savings: Savings, effort: Effort, control: str, length: int) -> None:
    keystrokes, seconds = effort.type_cost(control, length)
    savings.keystrokes_by_hand += keystrokes
    savings.seconds_by_hand += seconds


# ----------------------------------------------------------------------
# many runs
# ----------------------------------------------------------------------


@dataclass
class Sweep:
    """The same simulation over many cases, so the saving is not anecdotal."""

    cases: int = 0
    seeds: list[str] = dc_field(default_factory=list)
    threshold: float = ACCEPT_ABOVE
    filled: int = 0
    left: int = 0
    checked: int = 0
    right: int = 0
    wrong: int = 0
    seconds_by_hand: float = 0.0
    seconds_now: float = 0.0
    keystrokes_by_hand: int = 0
    keystrokes_now: int = 0

    @property
    def accuracy(self) -> float:
        return self.right / self.checked if self.checked else 0.0

    @property
    def seconds_saved(self) -> float:
        return self.seconds_by_hand - self.seconds_now

    @property
    def share_saved(self) -> float:
        if self.seconds_by_hand <= 0:
            return 0.0
        return self.seconds_saved / self.seconds_by_hand

    def per_case(self, total: float) -> float:
        return total / self.cases if self.cases else 0.0

    def headline(self) -> str:
        return (
            f"over {self.cases} forms, typing {len(self.seeds)} field(s) each: "
            f"{self.per_case(self.filled):.0f} of "
            f"{self.per_case(self.filled + self.left):.0f} fields filled at "
            f"{self.accuracy * 100:.0f}% accuracy, "
            f"{spell_out(self.per_case(self.seconds_now))} a form against "
            f"{spell_out(self.per_case(self.seconds_by_hand))} by hand "
            f"({_share_words(self.share_saved)})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "seeds": list(self.seeds),
            "threshold": self.threshold,
            "filled": self.filled,
            "left": self.left,
            "checked": self.checked,
            "right": self.right,
            "wrong": self.wrong,
            "accuracy": round(self.accuracy, 4),
            "share_saved": round(self.share_saved, 4),
            "seconds_by_hand": round(self.seconds_by_hand, 1),
            "seconds_now": round(self.seconds_now, 1),
            "seconds_saved": round(self.seconds_saved, 1),
            "keystrokes_saved": self.keystrokes_by_hand - self.keystrokes_now,
            "per_case": {
                "filled": round(self.per_case(self.filled), 2),
                "left": round(self.per_case(self.left), 2),
                "by_hand": spell_out(self.per_case(self.seconds_by_hand)),
                "now": spell_out(self.per_case(self.seconds_now)),
                "saved": spell_out(self.per_case(self.seconds_saved)),
            },
            "headline": self.headline(),
        }


def sweep(model: AutofillModel, cases: list[dict[str, Any]], seeds: list[str], *,
          threshold: float = ACCEPT_ABOVE, effort: Effort = DEFAULT_EFFORT) -> Sweep:
    """Run the simulation over many cases, typing ``seeds`` from each.

    One form is an anecdote. This is the same run repeated over records the
    model has not seen, which is the number worth quoting to anyone deciding
    whether to put this in front of an agent.
    """
    seeds = [s for s in seeds if s in model.profiles]
    total = Sweep(seeds=list(seeds), threshold=threshold)

    for case in cases:
        typed = {}
        for name in seeds:
            value = features.normalise(case.get(name))
            if value:
                typed[name] = value
        result = run(model, typed, case=case, threshold=threshold, effort=effort)
        total.cases += 1
        total.filled += result.savings.filled
        total.left += result.savings.left
        total.seconds_by_hand += result.savings.seconds_by_hand
        total.seconds_now += result.savings.seconds_now
        total.keystrokes_by_hand += result.savings.keystrokes_by_hand
        total.keystrokes_now += result.savings.keystrokes_now
        if result.score:
            total.checked += result.score.checked
            total.right += result.score.right
            total.wrong += result.score.wrong
    return total
