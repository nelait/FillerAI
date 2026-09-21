"""The original engine: every field votes with its own conditional table.

This is the model FillerAI shipped with, kept as one of the choices because
on a form it is hard to beat and impossible to misread. For each target it
finds the handful of other fields that genuinely predict it - scored with
Goodman and Kruskal's lambda, leave-one-out, against a shuffled baseline, all
of which lives in :mod:`..associate` - and stores the conditional
distribution for each. Asked to predict, every stored predictor whose field
the agent has typed votes with its own table, weighted by how much it has
been shown to help and by how many rows back the particular bucket being
read.

**Where it is strong.** Every number in it is countable by hand. "home_city
= Austin means home_state = TX in 94% of 213 records" is not an
approximation of the reason, it is the reason, and an agent reading it can
tell whether to trust it. It also degrades gracefully: one typed field is
enough for it to say something, and each further field narrows the answer.

**Where it is weak, and why the tree exists.** It is a vote of one-field
opinions, so it cannot represent a rule that needs two fields *together*.
"Policy type is family and the claimant is a dependent, therefore the
relationship box is child" is invisible to it: neither field alone shifts
the answer much, and averaging two weak opinions keeps it weak. That is
exactly the shape a decision tree captures, which is what the other engines
in this package are for.
"""

from __future__ import annotations

from typing import Any

from .. import associate
from ..associate import Link
from ..features import Profile
from .base import Algorithm, Ballot, Engine, FitContext, Guess, register


class StatisticalEngine(Engine):
    """Per-target conditional tables, combined by a weighted vote."""

    algorithm = "statistical"

    def __init__(self, links: dict[str, list[Link]]) -> None:
        self.links = links

    # -- prediction --------------------------------------------------------

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        ballot = Ballot(self.combiner)
        for link in self.links.get(name, []):
            value = known.get(link.source)
            if not value:
                continue
            distribution, rows = link.conditional(value)
            if not distribution:
                continue
            weight = link.strength * associate.support_weight(rows)
            top = max(distribution, key=lambda candidate: distribution[candidate])
            ballot.cast(
                distribution, weight,
                reason=(f"{link.source} = {value} -> {top} in "
                        f"{distribution[top] * 100:.0f}% of {rows} records"),
                strength=link.strength, support=rows,
            )
        ballot.floor(profile)
        return ballot.guess()

    # -- description -------------------------------------------------------

    def sources(self, name: str) -> list[str]:
        return [link.source for link in self.links.get(name, [])]

    def strength(self, name: str) -> float:
        links = self.links.get(name)
        return links[0].strength if links else 0.0

    def reach(self, name: str, available: set[str]) -> float:
        best = 0.0
        for link in self.links.get(name, []):
            if link.source in available and link.strength > best:
                best = link.strength
        return best

    def summary(self) -> dict[str, Any]:
        links = [link for group in self.links.values() for link in group]
        return {
            "relationships": len(links),
            "fields with a predictor": len(self.links),
            "strongest": round(max((l.strength for l in links), default=0.0), 3),
        }

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "links": [link.to_dict() for group in self.links.values() for link in group],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StatisticalEngine":
        return cls(links_from(data.get("links") or []))


def links_from(raw: list[dict[str, Any]]) -> dict[str, list[Link]]:
    """Group serialised links by the field they predict, best first."""
    links: dict[str, list[Link]] = {}
    for entry in raw:
        link = Link.from_dict(entry)
        links.setdefault(link.target, []).append(link)
    for group in links.values():
        group.sort(key=lambda l: (-l.strength, -l.support, l.source))
    return links


def fit(context: FitContext) -> StatisticalEngine:
    trace = context.trace
    options = context.options
    min_lambda = getattr(options, "min_lambda", associate.MIN_LAMBDA)
    max_predictors = getattr(options, "max_predictors", associate.MAX_PREDICTORS)

    usable = context.usable()
    trace.step(
        f"looking for relationships among {len(usable)} fields "
        f"({len(usable) * (len(usable) - 1)} ordered pairs to score)"
    )
    links = associate.learn_links(
        context.columns, context.profiles,
        min_lambda=min_lambda, max_predictors=max_predictors,
        trace=trace,
    )
    kept = sum(len(group) for group in links.values())
    trace.done(
        f"kept {kept} relationship(s) over {len(links)} field(s), "
        f"at lambda >= {min_lambda}"
    )
    for target, group in sorted(links.items(), key=lambda kv: -kv[1][0].strength)[:8]:
        best = group[0]
        trace.detail(f"  {target} <- {best.source} (lambda {best.strength:.2f}, "
                     f"{best.support} records)")
    return StatisticalEngine(links)


ALGORITHM = register(Algorithm(
    name="statistical",
    label="Conditional tables (statistical)",
    blurb="Counts how each field's values co-occur with each other field's, keeps "
          "the pairs that genuinely predict, and lets them vote. Every number in "
          "the result can be checked by hand.",
    recipe=[
        "Profile each column: how full it is, and whether its values form a set "
        "small enough to choose from.",
        "For every ordered pair of those fields, bucket the records by the "
        "predictor's value and count what the target held.",
        "Score the pair with Goodman and Kruskal's lambda: the share of guessing "
        "errors that knowing the predictor removes.",
        "Score each row against its bucket with that row held out, so a field "
        "with a unique value per record earns nothing for memorising.",
        "Score it again with the predictor column shuffled, and subtract that, so "
        "bucket structure alone is not mistaken for a relationship.",
        "Keep the best few predictors per field and store their conditional tables.",
        "To predict, let every predictor the agent has typed vote with its table, "
        "weighted by its lambda and by how many records back its bucket.",
    ],
    fit=fit,
    load=StatisticalEngine.from_dict,
    knobs=[
        ("min_lambda", "Weakest relationship to keep", "float",
         associate.MIN_LAMBDA, 0.0, 0.9),
        ("max_predictors", "Predictors per field", "int",
         associate.MAX_PREDICTORS, 1, 12),
    ],
))
