"""What every training algorithm has to be, and what it is allowed to assume.

The model as a whole is not one algorithm. Three things answer for a field,
and only the middle one is up for choice:

1. A **rule**, verified against the data in :mod:`..derive`. An age that is
   arithmetic on a date of birth is arithmetic whichever algorithm is
   selected, so rules sit above the choice rather than inside it.
2. An **engine** - the selectable part. Given the fields an agent has typed,
   it says what the rest of the form probably holds and how sure it is.
3. The field's **usual value**, as a floor, when the engine has nothing.

So an algorithm here is not a whole model. It is the answer to one question:
*given some fields of this form, what are the others?* Everything else -
profiling the columns, verifying the rules, holding out records, calibrating
the confidence, scoring the result - is shared, which is what makes two
algorithms comparable on the same form.

**The shape of the problem is what rules the textbook implementations out.**
A classifier is trained once on a fixed set of features and asked about all
of them. Here the features are whichever boxes the agent happened to fill
first, the target is every other box on the form, and the two swap places
from one record to the next. An engine is therefore fitted per target field
and has to answer from an arbitrary subset of the others - including, on the
first keystroke, almost none of them. An engine that needs its inputs
present is an engine that never fires.

Each engine is judged on the same held-out records by the same calibration,
so a confidence of 0.8 means the same thing whichever one produced it. That
is the point of the shared layer: the number a user reads stays comparable
when they change the dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Iterable

from ...schema import FormSchema
from ..features import Profile
from ..trace import Trace, resolve


@dataclass
class Guess:
    """One engine's answer about one field.

    ``score`` is raw - a probability under that engine's own assumptions,
    which are wrong in their own particular way. The calibration above turns
    it into the number a user is shown, and does so from measurements, which
    is what lets a naive Bayes score and a forest score be read off the same
    scale.
    """

    value: str | None = None
    score: float = 0.0
    because: list[str] = dc_field(default_factory=list)
    alternatives: list[tuple[str, float]] = dc_field(default_factory=list)
    # Distribution over candidates, when the engine has one. The model uses
    # it to mix in the marginal floor; engines that cannot produce one (a
    # nearest-neighbour copy of a single record, say) leave it empty.
    distribution: dict[str, float] = dc_field(default_factory=dict)
    # True when the answer leaned on something the caller actually typed, as
    # opposed to falling back on what the field usually says. Drives the
    # "answered by other fields" versus "answered by its usual value"
    # distinction in every report.
    used_evidence: bool = False

    @property
    def known(self) -> bool:
        return self.value is not None


@dataclass
class FitContext:
    """Everything an engine is handed to learn from.

    Columns and profiles are computed once and shared, so five algorithms
    fitted on one dataset do not profile it five times - and, more
    importantly, so they all agree about which fields are even worth
    predicting.
    """

    schema: FormSchema
    records: list[dict[str, Any]]
    columns: dict[str, list[str]]
    profiles: dict[str, Profile]
    trace: Trace
    options: Any = None  # TrainOptions, untyped here to avoid a cycle

    def usable(self) -> list[str]:
        """Fields with a closed enough set of values to be chosen from.

        An open field - a claim number, a free-text description - is neither
        predictable nor useful as a key into anything, because its values are
        effectively unique. Every engine draws the line in the same place.
        """
        return [
            name for name, profile in self.profiles.items()
            if profile.kind in ("constant", "enumerable") and profile.filled > 0
        ]


class Engine:
    """The selectable half of a model, fitted and serialisable.

    Subclasses implement :meth:`guess`, :meth:`to_dict` and
    :meth:`from_dict`, and usually :meth:`sources` so the field report can
    say where an answer came from.
    """

    #: Matches the ``name`` of the algorithm that produced it, so a saved
    #: model reloads into the same engine class.
    algorithm = "none"

    # -- prediction --------------------------------------------------------

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        """What field ``name`` probably holds, given the fields in ``known``.

        ``profile`` is the target column as the shared layer described it, so
        an engine can mix in what the field usually says without keeping its
        own copy of that. Every engine is expected to use it as a floor: an
        answer of "the common value, and not confidently" beats no answer.
        """
        raise NotImplementedError

    def targets(self) -> list[str]:
        """Fields this engine was fitted for. Empty means "all of them"."""
        return []

    def sources(self, name: str) -> list[str]:
        """Fields this engine leans on for ``name``, best first."""
        return []

    def strength(self, name: str) -> float:
        """How well it expects to do on ``name``, 0..1, before any evidence."""
        return 0.0

    def reach(self, name: str, available: set[str]) -> float:
        """Best it could do for ``name`` knowing only ``available``.

        Used to choose which fields are worth asking an agent to type first.
        The default reads the stored sources, which is right for anything
        that records per-field predictors; engines that work off whole
        records override it.
        """
        best = 0.0
        if any(source in available for source in self.sources(name)):
            best = self.strength(name)
        return best

    # -- description -------------------------------------------------------

    def explain(self, name: str) -> str:
        """One line for the field report about how ``name`` gets answered."""
        sources = self.sources(name)
        return "from " + ", ".join(sources[:3]) if sources else ""

    def summary(self) -> dict[str, Any]:
        """Numbers a UI can show about the fitted engine itself.

        Every engine reports something different and that is the point: a
        forest has trees and depths, a nearest-neighbour has neighbours and
        rows kept. The UI renders whatever comes back rather than knowing
        about any of them.
        """
        return {}

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Engine":
        raise NotImplementedError


@dataclass
class Algorithm:
    """One selectable way of learning a form, and how to say so."""

    name: str
    label: str
    #: One sentence for the picker.
    blurb: str
    #: What it does, in the order it does it - shown beside the log so a
    #: reader can follow along, and the honest place to put the caveats.
    recipe: list[str]
    #: Builds a fitted engine from the shared context.
    fit: Callable[[FitContext], Engine]
    #: Rebuilds one from its serialised form.
    load: Callable[[dict[str, Any]], Engine]
    #: Knobs this algorithm reads off TrainOptions.tuning, for the UI to
    #: offer. Each is (key, label, kind, default, low, high).
    knobs: list[tuple[str, str, str, Any, Any, Any]] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "blurb": self.blurb,
            "recipe": list(self.recipe),
            "knobs": [
                {"key": k, "label": lbl, "kind": kind,
                 "default": default, "low": low, "high": high}
                for k, lbl, kind, default, low, high in self.knobs
            ],
        }


_REGISTRY: dict[str, Algorithm] = {}


def register(algorithm: Algorithm) -> Algorithm:
    _REGISTRY[algorithm.name] = algorithm
    return algorithm


def get(name: str) -> Algorithm:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"no training algorithm called {name!r}; try one of {known}") from None


def names() -> list[str]:
    return list(_REGISTRY)


def all_algorithms() -> list[Algorithm]:
    return list(_REGISTRY.values())


# ----------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------


def tuning(context: FitContext, key: str, default: Any) -> Any:
    """Read one knob off the options, falling back to the algorithm's default."""
    options = context.options
    values = getattr(options, "tuning", None) or {}
    value = values.get(key, default)
    if isinstance(default, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def ranked(distribution: dict[str, float], keep: int = 5) -> list[tuple[str, float]]:
    """Candidates best first, ties broken by name so a model is diffable."""
    return sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))[:keep]


def normalised(scores: dict[str, float]) -> dict[str, float]:
    total = sum(scores.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in scores.items()}


def progress_every(trace: Trace | None, items: Iterable[Any], every: int,
                   message: Callable[[int, Any], str]) -> Iterable[Any]:
    """Yield ``items``, logging every ``every`` of them.

    A wide form has forty-odd targets and the per-target work is the slow
    part, so a line every few fields is the difference between a log that
    reads as progress and one that arrives all at once at the end.
    """
    trace = resolve(trace)
    for index, item in enumerate(items):
        if index and index % every == 0:
            trace.detail(message(index, item))
        yield item


# The marginal always gets a vote, so a field with no evidence pointing at it
# still answers with the common value rather than nothing. Small enough that
# one decent predictor outvotes it.
MARGINAL_WEIGHT = 0.2


class Ballot:
    """Weighted votes over what a field might hold.

    Every engine ends up doing the same last step - several opinions about
    one field, each deserving more or less weight, averaged into something
    that still reads as a probability - so it is written once here.

    **Averaged, not multiplied.** Multiplying independent likelihoods is the
    textbook move and it is wrong on a form. Form fields are not independent
    - city, state and ZIP are three views of one fact - so multiplying counts
    the same evidence three times and returns 0.999 for answers that are
    merely popular. A weighted average cannot do that: with every voter
    agreeing, the result is still just their shared probability, and the
    number stays interpretable as a confidence. Which matters, because the
    whole point is telling an agent what to check.
    """

    def __init__(self) -> None:
        self.scores: dict[str, float] = {}
        self.weight = 0.0
        self.because: list[str] = []
        self.evidence = False

    def cast(self, distribution: dict[str, float], weight: float,
             reason: str | None = None, evidence: bool = True) -> None:
        if weight <= 0 or not distribution:
            return
        for candidate, probability in distribution.items():
            self.scores[candidate] = self.scores.get(candidate, 0.0) + weight * probability
        self.weight += weight
        self.evidence = self.evidence or evidence
        if reason:
            self.because.append(reason)

    def floor(self, profile: Profile, weight: float = MARGINAL_WEIGHT) -> None:
        """Mix in what the field usually says, so thin evidence backs off.

        Without this a bucket of two rows commits to whatever those two rows
        held. With it, an engine that has barely seen a case returns the
        common answer at a modest score, which is both more often right and
        more honest about why.
        """
        self.cast(profile.distribution(), weight, evidence=False)

    def result(self) -> dict[str, float]:
        if self.weight <= 0:
            return {}
        return {k: v / self.weight for k, v in self.scores.items()}

    def guess(self, keep: int = 5) -> Guess:
        distribution = self.result()
        if not distribution:
            return Guess()
        order = ranked(distribution, keep + 1)
        value, score = order[0]
        return Guess(
            value=value, score=score, because=list(self.because),
            alternatives=order[1:keep + 1], distribution=distribution,
            used_evidence=self.evidence,
        )
