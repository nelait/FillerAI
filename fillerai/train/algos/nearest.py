"""Find the records most like this one, and copy what they said.

This is the engine that matches what the job actually is. An agent finishing
a claims form is not running a classifier in their head; they are thinking
"this is another commercial auto claim from the same broker, and those ones
go like this". Nearest neighbours does that literally: hold on to the past
records, and when a new form comes in, find the handful that agree with what
has been typed so far and let them fill the rest.

Two things make it work here rather than merely run.

**The fields are weighted per target.** Plain Hamming distance treats "the
country matches" as worth as much as "the group number matches", and on a
form where every record says United States the first is worth nothing.
Each field is weighted by how much it narrows the particular field being
predicted, from the cheap gain-ratio shortlist in :mod:`.relevance`, so the
neighbours that count are the ones that agree about things that matter.

**A rare agreement counts for more than a common one.** Two records both
saying "Texas" is weak evidence they are alike; two both saying "Wyoming" is
strong. So a match is scaled by how surprising the matched value is, which
is the same inverse-frequency idea that makes text search work.

Its honest weakness is that it is the one engine whose model file is the
training records - a bounded sample of them, but still rows rather than
counts. On generated data that is nobody's business; on real past
submissions it is very much somebody's, so the sample is capped, and the
whole point of FillerAI's generate stage is that this need never hold real
rows at all.
"""

from __future__ import annotations

import math
import random
from typing import Any

from ..features import Profile
from .base import Algorithm, Ballot, Engine, FitContext, Guess, register, tuning
from . import relevance

# The rows kept in the model. A form dataset is hundreds of records, and a
# neighbour search over more than this is slower per keystroke without being
# better - the nearest few are already the nearest few.
MAX_ROWS = 600

# How many neighbours vote, by default. One neighbour is a copy of a single
# record, with every idiosyncrasy of it; too many and the vote is the
# marginal wearing a hat.
NEIGHBOURS = 12

# A neighbour has to agree about at least this share of the weighted evidence
# before it is allowed an opinion.
MIN_SIMILARITY = 0.15

# Rows used to score the finished search. Scoring is a search per row per
# field, so this is the one number here that decides how long a fit takes.
MAX_MEASURED = 120


class NearestEngine(Engine):
    """A bounded sample of past records, searched by weighted agreement."""

    algorithm = "nearest"

    def __init__(self, fields: list[str], rows: list[list[str]],
                 weights: dict[str, dict[str, float]],
                 surprise: dict[str, dict[str, float]],
                 neighbours: int = NEIGHBOURS,
                 strengths: dict[str, float] | None = None) -> None:
        self.fields = fields
        self.rows = rows
        self.weights = weights  # target -> source -> relevance
        self.surprise = surprise  # field -> value -> -log2(share)
        self.neighbours = neighbours
        self.strengths = strengths or {}
        self._index = {name: position for position, name in enumerate(fields)}

    # -- prediction --------------------------------------------------------

    def _weight_of(self, target: str, source: str, value: str) -> float:
        relevance_weight = (self.weights.get(target) or {}).get(source, 0.0)
        if relevance_weight <= 0:
            return 0.0
        rarity = (self.surprise.get(source) or {}).get(value)
        if rarity is None:
            # A value this field never held is maximally surprising, and
            # correspondingly untrustworthy as evidence of similarity. One
            # bit is the smallest honest amount.
            rarity = 1.0
        return relevance_weight * max(rarity, 0.25)

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        target_index = self._index.get(name)
        ballot = Ballot(self.combiner)
        if target_index is None:
            ballot.floor(profile)
            return ballot.guess()

        evidence = [
            (self._index[source], source, value,
             self._weight_of(name, source, value))
            for source, value in known.items()
            if source in self._index and value
        ]
        evidence = [item for item in evidence if item[3] > 0]
        total_weight = sum(weight for _, _, _, weight in evidence)

        if total_weight > 0:
            scored: list[tuple[float, str]] = []
            for row in self.rows:
                answer = row[target_index]
                if not answer:
                    continue
                agreed = sum(weight for index, _source, value, weight in evidence
                             if row[index] == value)
                similarity = agreed / total_weight
                if similarity >= MIN_SIMILARITY:
                    scored.append((similarity, answer))
            scored.sort(key=lambda pair: (-pair[0], pair[1]))
            chosen = scored[:self.neighbours]
            if chosen:
                votes: dict[str, float] = {}
                for similarity, answer in chosen:
                    # Squared, so a neighbour that agrees about everything
                    # counts for meaningfully more than one that agrees about
                    # half. Linear weighting lets a crowd of half-matches
                    # outvote the record that is actually the same case.
                    votes[answer] = votes.get(answer, 0.0) + similarity ** 2
                mass = sum(votes.values())
                distribution = {k: v / mass for k, v in votes.items()}
                agreement = sum(s for s, _ in chosen) / len(chosen)
                top = max(distribution, key=lambda c: distribution[c])
                ballot.cast(
                    distribution,
                    self.strengths.get(name, 0.5) * agreement,
                    reason=(f"{len(chosen)} similar record(s), {agreement * 100:.0f}% "
                            f"agreement -> {top} in "
                            f"{distribution[top] * 100:.0f}% of them"),
                    strength=self.strengths.get(name, 0.5),
                    support=len(chosen),
                )
        ballot.floor(profile)
        return ballot.guess()

    # -- description -------------------------------------------------------

    def targets(self) -> list[str]:
        return list(self.weights)

    def sources(self, name: str) -> list[str]:
        scores = self.weights.get(name) or {}
        return [f for f, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]

    def strength(self, name: str) -> float:
        return self.strengths.get(name, 0.0)

    def reach(self, name: str, available: set[str]) -> float:
        scores = self.weights.get(name) or {}
        total = sum(scores.values())
        if total <= 0:
            return 0.0
        covered = sum(weight for source, weight in scores.items() if source in available)
        return self.strength(name) * (covered / total)

    def explain(self, name: str) -> str:
        sources = self.sources(name)
        return ("matched on " + ", ".join(sources[:3])) if sources else ""

    def summary(self) -> dict[str, Any]:
        return {
            "records kept": len(self.rows),
            "fields matched on": len(self.fields),
            "neighbours consulted": self.neighbours,
            "best field": round(max(self.strengths.values(), default=0.0), 3),
        }

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "fields": list(self.fields),
            "rows": [list(row) for row in self.rows],
            "weights": {t: {s: round(w, 4) for s, w in group.items()}
                        for t, group in self.weights.items()},
            "surprise": {f: {v: round(s, 4) for v, s in group.items()}
                         for f, group in self.surprise.items()},
            "neighbours": self.neighbours,
            "strengths": {k: round(v, 4) for k, v in self.strengths.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NearestEngine":
        return cls(
            fields=[str(f) for f in data.get("fields") or []],
            rows=[[str(v) for v in row] for row in data.get("rows") or []],
            weights={str(t): {str(s): float(w) for s, w in group.items()}
                     for t, group in (data.get("weights") or {}).items()},
            surprise={str(f): {str(v): float(s) for v, s in group.items()}
                      for f, group in (data.get("surprise") or {}).items()},
            neighbours=int(data.get("neighbours", NEIGHBOURS)),
            strengths={str(k): float(v) for k, v in (data.get("strengths") or {}).items()},
        )


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def _surprise(profiles: dict[str, Profile], fields: list[str]) -> dict[str, dict[str, float]]:
    """``-log2(share)`` per value: how much an agreement about it is worth."""
    out: dict[str, dict[str, float]] = {}
    for name in fields:
        distribution = profiles[name].distribution()
        if not distribution:
            continue
        out[name] = {
            value: -math.log2(share) if share > 0 else 1.0
            for value, share in distribution.items()
        }
    return out


def _measure(engine: NearestEngine, columns: dict[str, list[str]],
             held: list[int], profiles: dict[str, Profile]) -> dict[str, float]:
    """How much guessing the search removes per field, on rows it does not hold.

    Measured the way the model is actually used - a handful of fields typed,
    the rest asked for - rather than with every field present, which no agent
    ever has.
    """
    strengths: dict[str, float] = {}
    if not held:
        return strengths

    for target in engine.targets():
        sources = engine.sources(target)[:3]
        if not sources:
            continue
        target_values = columns[target]
        counts: dict[str, int] = {}
        rows = [row for row in held if target_values[row]]
        if len(rows) < 3:
            continue
        for row in rows:
            counts[target_values[row]] = counts.get(target_values[row], 0) + 1
        majority = max(counts.values()) / len(rows)

        hits = 0
        for row in rows:
            known = {source: columns[source][row] for source in sources
                     if columns[source][row]}
            if not known:
                continue
            guess = engine.guess(target, known, profiles[target])
            if guess.value == target_values[row]:
                hits += 1
        accuracy = hits / len(rows)
        strengths[target] = (0.0 if majority >= 1.0
                             else max(0.0, (accuracy - majority) / (1 - majority)))
    return strengths


def fit(context: FitContext) -> NearestEngine:
    trace = context.trace
    usable = context.usable()
    keep = max(50, min(MAX_ROWS, tuning(context, "rows", MAX_ROWS)))
    neighbours = max(1, min(50, tuning(context, "neighbours", NEIGHBOURS)))

    trace.step(f"ranking which fields are worth matching on, over {len(usable)} fields")
    weights = relevance.table(context.columns, usable)
    trace.detail(f"  {len(weights)} field(s) have something worth matching on")

    # A fifth of the rows are never put in the index, so the engine can be
    # scored on records it cannot simply find. Reserved before the sample is
    # taken rather than after: on a dataset smaller than the cap, sampling
    # first leaves nothing to score against and every field reads as useless.
    total_rows = len(context.records)
    order = list(range(total_rows))
    random.Random(getattr(context.options, "seed", None) or 0).shuffle(order)
    reserve = min(MAX_MEASURED, max(1, total_rows // 5)) if total_rows >= 10 else 0
    held, searchable = order[:reserve], order[reserve:]

    step = max(1, len(searchable) // keep)
    sample = searchable[::step][:keep]
    trace.step(f"keeping {len(sample)} of {total_rows} record(s) to search, "
               f"{len(held)} held back to score against")

    rows = [[context.columns[name][row] for name in usable] for row in sample]
    engine = NearestEngine(
        fields=usable, rows=rows, weights=weights,
        surprise=_surprise(context.profiles, usable), neighbours=neighbours,
    )

    # Scored on rows the search does not hold, so a field is never credited
    # for finding itself.
    trace.step(f"scoring the search on {len(held)} record(s) it does not hold")
    engine.strengths = _measure(engine, context.columns, held, context.profiles)
    best = sorted(engine.strengths.items(), key=lambda kv: -kv[1])[:8]
    trace.done(f"{len(engine.strengths)} field(s) scored")
    for name, strength in best:
        trace.detail(f"  {name}: removes {strength * 100:.0f}% of the guessing")
    return engine


ALGORITHM = register(Algorithm(
    name="nearest",
    label="Nearest records",
    blurb="Keeps a sample of the records and, for each new form, finds the ones "
          "that agree with what has been typed - then copies what they said. "
          "Closest to how an experienced agent actually works.",
    recipe=[
        "Rank, per field, which other fields are worth matching on.",
        "Weight every value by how surprising it is, so agreeing about a rare "
        "value counts for more than agreeing about a common one.",
        "Keep a bounded sample of the records as the model.",
        "To predict, score every kept record by how much of the typed evidence "
        "it agrees with, weighted both ways.",
        "Take the closest dozen and let them vote, each weighted by the square of "
        "its agreement so a real match outweighs a crowd of half-matches.",
        "Score the whole search on records it does not hold, one field at a time.",
    ],
    fit=fit,
    load=NearestEngine.from_dict,
    knobs=[
        ("neighbours", "Neighbours consulted", "int", NEIGHBOURS, 1, 50),
        ("rows", "Records kept", "int", MAX_ROWS, 50, MAX_ROWS),
    ],
))
