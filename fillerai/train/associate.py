"""What one field tells you about another.

This is the statistical half of the model. For every pair of enumerable
fields it counts how their values co-occur, then scores how much knowing the
first actually helps with the second.

The score is Goodman and Kruskal's lambda: the share of guessing errors that
knowing the predictor removes.

    lambda = (sum_x max_v n(x, v) - max_v n(v)) / (N - max_v n(v))

Plain accuracy would be the wrong measure here. A form where 94% of records
say "United States" makes *every* field look like a brilliant predictor of
country, because always guessing the majority is already right 94% of the
time. Lambda scores that at zero, which is the truth: the predictor added
nothing. Only a field that beats the majority guess earns weight.

Lambda as written above has a second failure, and it is the dangerous one.
Scored on the same rows the counts were built from, a field with a nearly
unique value per record gets a perfect score for memorising: every bucket
holds one row, and every bucket predicts that row exactly. A ZIP code then
looks like a flawless predictor of a *different* address's state.

Two corrections, and both are needed:

*Leave one out.* Each row is scored against the bucket with itself removed,
so a bucket of one - the memoriser's whole trick - is worth nothing.

*Beat a shuffle.* Leaving a row out still leaves luck. Where a predictor
splits the rows into buckets of two or three, some of those buckets agree by
chance, and on a field with four or five values that happens often enough to
look like a relationship. So the same score is computed again against the
predictor column shuffled, which keeps every bucket size intact and destroys
only the association. Whatever the shuffled column scores is what bucket
structure alone is worth, and the real column has to beat it before any of
it counts.

Together these are the difference between a model that knows a ZIP implies a
city and one that believes a diagnosis code implies a courtesy title.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .features import Profile
from .trace import Trace, resolve

# Per predictor value, only the most common target values are kept. The tail
# of a conditional distribution never wins a prediction, and dropping it is
# what keeps a model over a 45-field form a readable JSON file rather than a
# megabyte of near-zero counts.
KEEP_PER_BUCKET = 8

# A pair has to clear this to be stored at all: a predictor that removes
# less than a tenth of the guessing error cannot outvote the marginal it is
# mixed with, so all it adds is the weight of its own lookup table - and
# those tables are most of a saved model. It also keeps the report readable,
# which matters more than it sounds: "injury reported, from middle initial"
# costs a reader's trust in the lines above it that are true.
MIN_LAMBDA = 0.1

# At most this many predictors per target, best first. A sixth opinion on a
# form field has never changed an answer; it only dilutes the good ones.
MAX_PREDICTORS = 6

# Evidence from a bucket seen twice should not count like evidence from a
# bucket seen two hundred times. weight *= n / (n + SUPPORT_SMOOTHING).
SUPPORT_SMOOTHING = 5.0

# A pair scored on fewer rows than this has not been tested, whatever it
# scored.
MIN_SUPPORT = 20

# How many shuffles the chance baseline is averaged over. One is enough to
# catch the effect and noisy about its size; three costs little and steadies
# it. This is the dominant cost of fitting, so it is not raised idly.
CHANCE_TRIALS = 3


@dataclass
class Link:
    """One directed predictor -> target relationship, with its table."""

    source: str
    target: str
    strength: float  # lambda, 0..1
    support: int  # records where both fields were filled
    # predictor value -> (rows in this bucket, [(target value, count), ...])
    table: dict[str, tuple[int, list[tuple[str, int]]]]

    def conditional(self, value: str) -> tuple[dict[str, float], int]:
        """``P(target | source = value)`` and how many rows back it.

        The kept head of the bucket rarely sums to its row count, so the
        missing tail is reported honestly by normalising over the bucket
        total rather than over the head. A prediction from a bucket whose
        tail is most of its mass then comes out appropriately unsure.
        """
        bucket = self.table.get(value)
        if not bucket:
            return {}, 0
        rows, pairs = bucket
        if rows <= 0:
            return {}, 0
        return {v: c / rows for v, c in pairs}, rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "strength": round(self.strength, 4),
            "support": self.support,
            "table": {k: [rows, [[v, c] for v, c in pairs]]
                      for k, (rows, pairs) in self.table.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Link":
        return cls(
            source=data["source"],
            target=data["target"],
            strength=float(data.get("strength", 0.0)),
            support=int(data.get("support", 0)),
            table={
                str(k): (int(rows), [(str(v), int(c)) for v, c in pairs])
                for k, (rows, pairs) in (data.get("table") or {}).items()
            },
        )


def support_weight(rows: int) -> float:
    """How much a bucket of ``rows`` observations deserves to be believed."""
    return rows / (rows + SUPPORT_SMOOTHING) if rows > 0 else 0.0


def _loo_hits(counter: Counter) -> float:
    """Rows this bucket predicts correctly with each row held out in turn.

    Take one row out, let the rest of the bucket vote, and ask whether the
    row's own value won. Where the vote ends in a k-way tie the row counts
    as ``1/k`` of a hit, because that is what a coin flip between k values
    is worth. Scoring a tie as a clean miss instead looks stricter and is
    not: on a target whose values are all about as common as each other,
    the *no-predictor* baseline is one long tie, and zeroing it hands every
    predictor credit for beating nothing.

    The bucket of one is the case that matters most. Removing its only row
    leaves nothing to vote, so it scores nothing - which is exactly what
    stops a field with a unique value per record from looking perfect.
    """
    if not counter:
        return 0.0
    total = sum(counter.values())
    if total <= 1:
        return 0.0

    counts = sorted(counter.values(), reverse=True)
    highest = counts[0]
    runner_up = counts[1] if len(counts) > 1 else 0
    tied_at_top = counts.count(highest)

    hits = 0.0
    for count in counter.values():
        # The best any other value in this bucket can offer.
        rival = runner_up if (count == highest and tied_at_top == 1) else highest
        remaining = count - 1
        if remaining > rival:
            hits += count
        elif remaining == rival:
            # Several other values can sit on the rival count too, so the
            # tie can be wider than two ways.
            winners = 1 + counts.count(rival) - (1 if count == rival else 0)
            hits += count / winners
    return hits


def _pair_counts(source: list[str], target: list[str]) -> tuple[dict[str, Counter], int]:
    """Bucket the rows where both fields were filled.

    A blank is the absence of evidence, not evidence of absence, so rows
    where either side is empty are simply not part of this pair's story.
    """
    joint: dict[str, Counter] = defaultdict(Counter)
    paired = 0
    for source_value, target_value in zip(source, target):
        if not source_value or not target_value:
            continue
        joint[source_value][target_value] += 1
        paired += 1
    return joint, paired


def _conditional_hits(joint: dict[str, Counter]) -> float:
    return sum(_loo_hits(counter) for counter in joint.values())


def _chance_hits(source: list[str], target: list[str], rng: random.Random) -> float:
    """What this predictor's bucket structure scores with the link destroyed.

    The predictor column is shuffled among the rows that took part, so the
    buckets keep their sizes and lose their meaning. Anything the real column
    does not beat was never a relationship.
    """
    pairs = [(s, t) for s, t in zip(source, target) if s and t]
    if not pairs:
        return 0.0
    sources = [s for s, _ in pairs]
    targets = [t for _, t in pairs]
    total = 0.0
    for _ in range(CHANCE_TRIALS):
        rng.shuffle(sources)
        joint, _paired = _pair_counts(sources, targets)
        total += _conditional_hits(joint)
    return total / CHANCE_TRIALS


def _lambda(joint: dict[str, Counter], target_counts: Counter, paired: int,
            chance: float) -> float:
    """The share of guessing errors the predictor removes, beyond luck.

    The bar is whichever is higher: always guessing the commonest value, or
    what this predictor's bucket sizes score with the association shuffled
    away. The first is the rule a predictor has to be worth more than; the
    second is what its shape alone is worth.

    Note that the commonest-value bar is *not* scored leave-one-out, and the
    per-bucket sums are. That asymmetry is deliberate rather than an
    oversight. Leaving a row out is a guard against memorising, and a rule
    with one parameter has nothing to memorise with - while applying it
    anyway introduces a cliff, since on a target whose two commonest values
    are neck and neck, removing a row of either hands the win to the other
    and the bar drops to zero. A predictor would then get credit for beating
    nothing at all.
    """
    if paired <= 0 or not target_counts:
        return 0.0
    baseline = max(float(max(target_counts.values())), chance)
    denominator = paired - baseline
    if denominator <= 0:
        # The target barely varies among these rows, or bucket structure
        # alone already explains it. Either way nothing was learned.
        return 0.0
    return max(0.0, min(1.0, (_conditional_hits(joint) - baseline) / denominator))


def learn_links(columns: dict[str, list[str]], profiles: dict[str, Profile],
                min_lambda: float = MIN_LAMBDA,
                max_predictors: int = MAX_PREDICTORS,
                trace: Trace | None = None) -> dict[str, list[Link]]:
    """Learn every worthwhile predictor, grouped by the field it predicts.

    Only enumerable fields take part. An open field is neither predictable
    from a table nor useful as a key into one, because its values are
    effectively unique and every bucket would hold a single row.
    """
    usable = [
        name for name, profile in profiles.items()
        if profile.kind in ("constant", "enumerable") and profile.filled > 0
    ]
    by_target: dict[str, list[Link]] = defaultdict(list)

    # Seeded rather than global, so fitting twice on the same data gives the
    # same model - a model you cannot diff is a model you cannot review.
    rng = random.Random(0)
    watcher = resolve(trace)

    for position, target in enumerate(usable, start=1):
        watcher.detail(f"  scoring predictors of {target} "
                       f"({position}/{len(usable)})")
        target_values = columns[target]
        for source in usable:
            if source == target:
                continue
            source_values = columns[source]

            joint, paired = _pair_counts(source_values, target_values)
            if paired < MIN_SUPPORT:
                continue
            paired_counts = Counter()
            for counter in joint.values():
                paired_counts.update(counter)

            chance = _chance_hits(source_values, target_values, rng)
            strength = _lambda(joint, paired_counts, paired, chance)
            if strength < min_lambda:
                continue
            table = {
                sv: (sum(counter.values()), counter.most_common(KEEP_PER_BUCKET))
                for sv, counter in joint.items()
            }
            by_target[target].append(
                Link(source=source, target=target, strength=strength,
                     support=paired, table=table)
            )

    for target, links in by_target.items():
        # Ties broken by support then name so a model fitted twice on the
        # same data is byte-identical, which is what makes it diffable.
        links.sort(key=lambda l: (-l.strength, -l.support, l.source))
        del links[max_predictors:]
    return dict(by_target)


def coverage_score(links_by_target: dict[str, list[Link]],
                   chosen: set[str]) -> dict[str, float]:
    """The best strength each target gets from the fields already chosen.

    Used to pick seed fields greedily: a candidate is worth adding when it
    raises this for targets the current choice leaves weak.
    """
    out: dict[str, float] = {}
    for target, links in links_by_target.items():
        best = 0.0
        for link in links:
            if link.source in chosen and link.strength > best:
                best = link.strength
        out[target] = best
    return out
