"""Measuring a model, and choosing what to ask the agent for.

Two questions, and they are the same question from opposite ends. *How good
is this?* is answered by filling held-out records from a few of their own
values and counting what came back right. *Which few?* is answered by
looking at what each field would unlock if it were the one typed.

Accuracy alone would be a poor headline. A model that answers three fields
perfectly and declines the other forty is not a good autofill model, so
coverage - how much of the form it was willing to fill - is reported beside
it, and neither number is allowed to hide the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from . import features
from .model import ACCEPT_ABOVE, AutofillModel

# Candidates considered at each greedy step. Forms get wide, and the tail of
# the ranking has never produced a pick.
MAX_CANDIDATES = 40

# See the gain calculation in suggest_seed_fields.
SELF_WORTH = 0.05


# ----------------------------------------------------------------------
# which fields to ask for
# ----------------------------------------------------------------------


def _derived_closure(model: AutofillModel, known: set[str]) -> set[str]:
    """Everything the rules can reach from ``known``, applied until settled.

    Rules chain: a full name derives from first and last, and a middle
    initial derives from the middle name. Asking once whether each rule can
    fire would miss the second round, so this runs to a fixed point.
    """
    reached = set(known)
    changed = True
    while changed:
        changed = False
        for target, rule in model.derivations.items():
            if target in reached:
                continue
            if all(name in reached for name in rule.inputs):
                reached.add(target)
                changed = True
    return reached


def _reach(model: AutofillModel, chosen: set[str]) -> dict[str, float]:
    """How well each field would be answered, given ``chosen`` was typed.

    The floor is what the field says without any help at all: a country box
    that reads "United States" in every record is already answered, and a
    seed field spent on it would buy nothing.

    A rule whose inputs are only partly covered scores a fraction of its
    accuracy rather than zero. That fraction is not a claim about what the
    model would actually do - with an input missing the rule does not fire
    at all - and it is not what any report shows. It exists so the greedy
    search below can climb towards a rule that needs more than one field.
    A full name derives from three boxes; scored strictly, adding the first
    of them buys nothing, so the second and third are never reached and the
    rule is never assembled.

    What the engine would manage is the engine's own answer, because the
    algorithms differ about it: a conditional table needs one particular
    field typed, a tree wants the fields its questions are about, and a
    nearest-record search improves with almost any of them. Each reports its
    own partial credit for the same reason the rules do.
    """
    available = _derived_closure(model, chosen)
    out: dict[str, float] = {}
    for name in model.targets():
        if name in chosen:
            continue
        best = 0.0
        rule = model.derivations.get(name)
        if rule is not None and rule.inputs:
            covered = sum(1 for i in rule.inputs if i in available)
            if covered == len(rule.inputs):
                out[name] = rule.accuracy
                continue
            best = rule.accuracy * covered / len(rule.inputs)

        profile = model.profiles[name]
        if profile.kind in ("constant", "enumerable"):
            modal = profile.modal()
            if modal and modal[1] > best:
                best = modal[1]
        learned = model.engine.reach(name, available)
        if learned > best:
            best = learned
        out[name] = best
    return out


def suggest_seed_fields(model: AutofillModel, count: int = 3) -> list[str]:
    """The fields worth asking an agent to type first.

    Greedy, and greedy is the right shape here: the value of a set of seed
    fields is how much of the form it covers between them, which grows by
    ever less as fields are added, so taking the best next one each time
    lands close to the best set and takes a fraction of the work.

    Fields the rules already produce are never suggested - typing a full
    name that derives from two boxes you are typing anyway is wasted effort.
    """
    chosen: list[str] = []
    pool = [
        name for name in model.targets()
        if name not in model.derivations and model.profiles[name].filled > 0
    ]
    # Fields that predict a lot, considered first; the rest rarely win a
    # round and looking at all of them on a wide form is not free.
    pool.sort(key=lambda n: (-_outgoing(model, n), n))
    pool = pool[:MAX_CANDIDATES]

    current = _reach(model, set())
    for _ in range(max(0, count)):
        best_name, best_gain, best_reach = None, 1e-9, None
        for candidate in pool:
            if candidate in chosen:
                continue
            reach = _reach(model, set(chosen) | {candidate})
            gain = sum(
                max(0.0, reach.get(n, 0.0) - current.get(n, 0.0))
                for n in model.targets() if n != candidate
            )
            # Typing a field is worth a little in itself, and only a
            # little: enough to break a tie towards a box that nothing else
            # can fill, never enough to outweigh what a field unlocks.
            gain += SELF_WORTH * (1.0 - current.get(candidate, 0.0))
            if gain > best_gain:
                best_name, best_gain, best_reach = candidate, gain, reach
        if best_name is None:
            break
        chosen.append(best_name)
        current = best_reach
    return chosen


def _outgoing(model: AutofillModel, name: str) -> float:
    """Total strength this field lends to others - how useful it is to know."""
    total = 0.0
    for target in model.targets():
        if name in model.engine.sources(target):
            total += model.engine.strength(target)
    for rule in model.derivations.values():
        if name in rule.inputs:
            total += rule.accuracy
    return total


# ----------------------------------------------------------------------
# measuring
# ----------------------------------------------------------------------


@dataclass
class FieldScore:
    name: str
    attempted: int = 0  # the model answered and the record had a truth to check
    correct: int = 0
    declined: int = 0  # the model had nothing to say
    confidence_sum: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.attempted if self.attempted else 0.0

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.attempted if self.attempted else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "attempted": self.attempted,
            "correct": self.correct,
            "declined": self.declined,
            "accuracy": round(self.accuracy, 4),
            "mean_confidence": round(self.mean_confidence, 4),
        }


@dataclass
class Report:
    """What the model did on records it was not fitted on."""

    seeds: list[str] = dc_field(default_factory=list)
    records: int = 0
    fields: list[FieldScore] = dc_field(default_factory=list)
    # Only predictions the model was confident enough to offer.
    accepted: int = 0
    accepted_correct: int = 0
    # Every prediction, whatever its confidence.
    attempted: int = 0
    correct: int = 0
    # Fields the model refused to answer, nearly always because their values
    # are different in every record. Reported rather than buried: "did not
    # answer" and "answered wrongly" are not the same failure.
    declined: int = 0
    checkable: int = 0  # fields with a truth to compare against
    threshold: float = ACCEPT_ABOVE
    reliability: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.attempted if self.attempted else 0.0

    @property
    def accepted_accuracy(self) -> float:
        return self.accepted_correct / self.accepted if self.accepted else 0.0

    @property
    def coverage(self) -> float:
        """Share of the fields still to fill that the model filled confidently."""
        return self.accepted / self.checkable if self.checkable else 0.0

    @property
    def offered(self) -> float:
        """Share it had any answer for, confident or not."""
        return self.attempted / self.checkable if self.checkable else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": list(self.seeds),
            "records": self.records,
            "threshold": self.threshold,
            "attempted": self.attempted,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "accepted": self.accepted,
            "accepted_correct": self.accepted_correct,
            "accepted_accuracy": round(self.accepted_accuracy, 4),
            "declined": self.declined,
            "checkable": self.checkable,
            "coverage": round(self.coverage, 4),
            "offered": round(self.offered, 4),
            "fields": [f.to_dict() for f in self.fields],
            "reliability": self.reliability,
            "headline": self.headline(),
        }

    def headline(self) -> str:
        """The result in one line, with the declined fields kept in view.

        Coverage on its own reads as a failing grade on a form that is mostly
        claim numbers. Saying how many fields nothing could have predicted is
        what makes the other numbers mean anything.
        """
        return (
            f"from {len(self.seeds)} field(s) across {self.records} records: "
            f"filled {self.coverage * 100:.0f}% of the remaining {self.checkable} "
            f"at {self.accepted_accuracy * 100:.0f}% accuracy; "
            f"{self.declined} had no answer to give"
        )


_RELIABILITY_BANDS = ((0.9, 1.01), (0.75, 0.9), (0.55, 0.75), (0.0, 0.55))


def evaluate(model: AutofillModel, records: list[dict[str, Any]],
             seeds: list[str] | int = 3,
             threshold: float = ACCEPT_ABOVE) -> Report:
    """Fill each record from its own seed fields and count what came back right.

    Comparison is on the normalised value, the same form the model was
    trained on, so a boolean written as ``true`` in one export and ``True``
    in another is not scored as a miss.
    """
    if isinstance(seeds, int):
        seeds = suggest_seed_fields(model, seeds)
    seeds = [s for s in seeds if s in model.profiles]

    report = Report(seeds=list(seeds), records=len(records), threshold=threshold)
    scores = {name: FieldScore(name=name) for name in model.targets()}
    bands = {band: [0, 0] for band in _RELIABILITY_BANDS}

    for record in records:
        known = {}
        for name in seeds:
            value = features.normalise(record.get(name))
            if value:
                known[name] = value

        for name in model.targets():
            if name in known:
                continue
            truth = features.normalise(record.get(name))
            if not truth:
                # Nothing to check against; a blank in the source says the
                # field was not applicable, not that the model was wrong.
                continue
            report.checkable += 1
            score = scores[name]
            prediction = model.predict_field(name, known)
            if not prediction.known:
                score.declined += 1
                report.declined += 1
                continue
            hit = prediction.value == truth
            score.attempted += 1
            score.confidence_sum += prediction.confidence
            report.attempted += 1
            if hit:
                score.correct += 1
                report.correct += 1
            if prediction.confidence >= threshold:
                report.accepted += 1
                if hit:
                    report.accepted_correct += 1
            for band in _RELIABILITY_BANDS:
                low, high = band
                if low <= prediction.confidence < high:
                    bands[band][0] += 1
                    bands[band][1] += int(hit)
                    break

    report.fields = [scores[name] for name in model.targets()]
    report.reliability = [
        {
            "from": low, "to": min(high, 1.0), "count": count, "correct": correct,
            "accuracy": round(correct / count, 4) if count else 0.0,
        }
        for (low, high), (count, correct) in bands.items()
    ]
    return report
