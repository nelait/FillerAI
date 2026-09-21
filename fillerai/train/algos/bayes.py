"""Naive Bayes, offered honestly - including about where it goes wrong.

The textbook classifier: assume every piece of evidence is independent given
the answer, multiply the likelihoods, normalise. It is here because it is the
first thing anyone asks for, it is genuinely quick and genuinely decent, and
because having it side by side with the others demonstrates something worth
seeing.

**The assumption it makes is false on a form, and visibly so.** City, state
and postal code are three views of one fact. Told all three, naive Bayes
multiplies what is really one piece of evidence three times and comes back at
0.999 for an answer it has one reason to believe. That is not a small effect:
it is why the conditional-table engine averages its voters instead of
multiplying them.

Two things keep it usable anyway, and they are the interesting part.

*Only the shortlisted fields multiply.* Rather than every field on the form,
each target uses the handful that :mod:`.relevance` says narrow it, which
both bounds the model and cuts the worst of the double counting - the fourth
and fifth view of the same fact are usually the ones that fall off the list.

*The confidence is measured, not computed.* The shared calibration above
fits how often predictions of a given raw score actually turn out right, on
records the model never saw. Naive Bayes' famous overconfidence is exactly
the sort of miscalibration that repairs: the raw 0.999 lands in the top bin,
the bin is measured at whatever it is really worth, and the number the user
reads is honest even though the number the model computed was not. Running
this algorithm and then looking at the reliability table is the clearest
demonstration in the project of what that calibration step is for.
"""

from __future__ import annotations

import math
from typing import Any

from ..features import Profile
from . import relevance
from .base import Algorithm, Ballot, Engine, FitContext, Guess, register, tuning

# Laplace smoothing. Without it one unseen combination sets a whole
# likelihood to zero and vetoes an answer the rest of the evidence supports.
ALPHA = 1.0

# Per target, at most this many fields multiply. Past a handful they are
# nearly all restatements of each other, which is precisely what this
# algorithm cannot handle.
MAX_EVIDENCE = 5


class BayesEngine(Engine):
    """Per-target likelihood tables, multiplied in log space."""

    algorithm = "bayes"

    def __init__(self, priors: dict[str, dict[str, float]],
                 tables: dict[str, dict[str, dict[str, dict[str, int]]]],
                 totals: dict[str, dict[str, int]],
                 strengths: dict[str, float] | None = None) -> None:
        # target -> value -> share
        self.priors = priors
        # target -> source -> target value -> source value -> count
        self.tables = tables
        # target -> target value -> rows counted
        self.totals = totals
        self.strengths = strengths or {}

    # -- prediction --------------------------------------------------------

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        prior = self.priors.get(name)
        tables = self.tables.get(name)
        if not prior or not tables:
            ballot = Ballot(self.combiner)
            ballot.floor(profile)
            return ballot.guess()

        used = [(source, value) for source, value in known.items()
                if value and source in tables]
        # Work in logs: a form with five pieces of evidence and a hundred
        # candidate values underflows a float in the ordinary way otherwise.
        scores: dict[str, float] = {}
        for candidate, share in prior.items():
            total = self.totals.get(name, {}).get(candidate, 0)
            log_probability = math.log(share if share > 0 else 1e-9)
            for source, value in used:
                counts = tables[source].get(candidate) or {}
                options = max(len(tables[source].get("__values__", {})), 2)
                log_probability += math.log(
                    (counts.get(value, 0) + ALPHA) / (total + ALPHA * options)
                )
            scores[candidate] = log_probability

        ballot = Ballot(self.combiner)
        if scores:
            top = max(scores.values())
            weights = {k: math.exp(v - top) for k, v in scores.items()}
            mass = sum(weights.values())
            distribution = {k: v / mass for k, v in weights.items()}
            if used:
                best = max(distribution, key=lambda c: distribution[c])
                shown = ", ".join(f"{source} = {value}" for source, value in used[:3])
                ballot.cast(
                    distribution, self.strengths.get(name, 0.5),
                    reason=(f"{shown} -> {best}, at {distribution[best] * 100:.0f}% "
                            f"before calibration"),
                    strength=self.strengths.get(name, 0.5),
                    support=sum((self.totals.get(name) or {}).values()),
                )
        ballot.floor(profile)
        return ballot.guess()

    # -- description -------------------------------------------------------

    def targets(self) -> list[str]:
        return list(self.priors)

    def sources(self, name: str) -> list[str]:
        return [s for s in (self.tables.get(name) or {})]

    def strength(self, name: str) -> float:
        return self.strengths.get(name, 0.0)

    def reach(self, name: str, available: set[str]) -> float:
        sources = self.sources(name)
        if not sources:
            return 0.0
        covered = sum(1 for source in sources if source in available)
        return self.strength(name) * (covered / len(sources))

    def explain(self, name: str) -> str:
        sources = self.sources(name)
        return ("multiplies " + ", ".join(sources[:3])) if sources else ""

    def summary(self) -> dict[str, Any]:
        return {
            "fields with evidence": len(self.tables),
            "likelihoods stored": sum(
                len(values) for group in self.tables.values()
                for source in group.values() for values in source.values()
            ),
            "best field": round(max(self.strengths.values(), default=0.0), 3),
        }

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "priors": {t: {v: round(p, 5) for v, p in group.items()}
                       for t, group in self.priors.items()},
            "tables": self.tables,
            "totals": self.totals,
            "strengths": {k: round(v, 4) for k, v in self.strengths.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BayesEngine":
        return cls(
            priors={str(t): {str(v): float(p) for v, p in group.items()}
                    for t, group in (data.get("priors") or {}).items()},
            tables={
                str(t): {
                    str(s): {str(tv): {str(sv): int(c) for sv, c in counts.items()}
                             for tv, counts in by_target.items()}
                    for s, by_target in group.items()
                }
                for t, group in (data.get("tables") or {}).items()
            },
            totals={str(t): {str(v): int(c) for v, c in group.items()}
                    for t, group in (data.get("totals") or {}).items()},
            strengths={str(k): float(v) for k, v in (data.get("strengths") or {}).items()},
        )


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def _measure(engine: BayesEngine, columns: dict[str, list[str]], held: list[int],
             profiles: dict[str, Profile]) -> dict[str, float]:
    """Errors removed per field, on records the counts were not built from."""
    strengths: dict[str, float] = {}
    for target in engine.targets():
        sources = engine.sources(target)[:3]
        target_values = columns[target]
        rows = [row for row in held if target_values[row]]
        if len(rows) < 5 or not sources:
            continue
        counts: dict[str, int] = {}
        for row in rows:
            counts[target_values[row]] = counts.get(target_values[row], 0) + 1
        majority = max(counts.values()) / len(rows)
        hits = 0
        for row in rows:
            known = {s: columns[s][row] for s in sources if columns[s][row]}
            if not known:
                continue
            if engine.guess(target, known, profiles[target]).value == target_values[row]:
                hits += 1
        accuracy = hits / len(rows)
        strengths[target] = (0.0 if majority >= 1.0
                             else max(0.0, (accuracy - majority) / (1 - majority)))
    return strengths


def fit(context: FitContext) -> BayesEngine:
    trace = context.trace
    columns = context.columns
    usable = context.usable()
    max_evidence = max(1, min(12, tuning(context, "evidence", MAX_EVIDENCE)))

    trace.step(f"shortlisting evidence for {len(usable)} field(s)")
    shortlist = relevance.table(columns, usable, keep=max_evidence)

    rows = len(context.records)
    fit_rows = list(range(int(rows * 0.8))) or list(range(rows))
    held = list(range(int(rows * 0.8), rows))

    trace.step(f"counting likelihoods over {len(fit_rows)} record(s)")
    priors: dict[str, dict[str, float]] = {}
    tables: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    totals: dict[str, dict[str, int]] = {}

    for position, (target, sources) in enumerate(shortlist.items(), start=1):
        target_values = columns[target]
        counted: dict[str, int] = {}
        for row in fit_rows:
            value = target_values[row]
            if value:
                counted[value] = counted.get(value, 0) + 1
        if not counted:
            continue
        seen = sum(counted.values())
        priors[target] = {value: count / seen for value, count in counted.items()}
        totals[target] = counted

        group: dict[str, dict[str, dict[str, int]]] = {}
        for source in sources:
            by_target: dict[str, dict[str, int]] = {}
            values: dict[str, int] = {}
            for row in fit_rows:
                answer, evidence = target_values[row], columns[source][row]
                if not answer or not evidence:
                    continue
                by_target.setdefault(answer, {})
                by_target[answer][evidence] = by_target[answer].get(evidence, 0) + 1
                values[evidence] = 1
            if by_target:
                # Stashed alongside the counts so the smoothing denominator
                # knows how many values the source can take, which is what
                # keeps an unseen combination from being scored as impossible.
                by_target["__values__"] = values
                group[source] = by_target
        if group:
            tables[target] = group
        if position % 10 == 0:
            trace.detail(f"  counted {position}/{len(shortlist)} field(s)")

    engine = BayesEngine(priors=priors, tables=tables, totals=totals)
    trace.step(f"scoring on {len(held)} held-back record(s)")
    engine.strengths = _measure(engine, columns, held, context.profiles)
    trace.done(
        f"{len(tables)} field(s) have evidence; the raw scores will read far too "
        f"confident until the calibration step measures them"
    )
    return engine


ALGORITHM = register(Algorithm(
    name="bayes",
    label="Naive Bayes",
    blurb="Multiplies the evidence together under the assumption that the fields "
          "are independent, which on a form they are not. Fast, decent at picking "
          "the right answer, and badly overconfident until it is calibrated.",
    recipe=[
        "Shortlist, per field, the handful of other fields that narrow it most.",
        "Count how often each shortlisted field's values appear beside each answer.",
        "Smooth the counts, so one unseen combination cannot veto an answer.",
        "To predict, start from how common each answer is and multiply in one "
        "likelihood per typed field, in log space.",
        "Normalise, and hand the result to the shared calibration - which is where "
        "the overconfidence gets corrected against held-out records.",
    ],
    fit=fit,
    load=BayesEngine.from_dict,
    knobs=[("evidence", "Fields multiplied per target", "int", MAX_EVIDENCE, 1, 12)],
))
