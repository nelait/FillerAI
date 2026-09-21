"""Weights, fitted: one softmax regression per field, and hashed evidence.

The other five engines learn by counting, by partitioning, or by keeping
records. This one learns a number per piece of evidence per candidate answer,
by gradient descent on the records - the thing every other engine here
deliberately is not. `docs/weight-based-training.md` is the analysis this was
built from, including the two places it is honestly worse than what exists.

**Why it is here.** Not for interactions - a decision tree captures those in a
fraction of the time. It is here for cardinality. A field with more than
``MAX_DISTINCT`` distinct values is classed ``open``, and the conditional
tables admit only enumerable fields as predictors, so a city column with two
thousand cities is invisible to them even when it fixes the state exactly.
More history makes *fewer* fields usable, which is backwards. A weight does
not need its field enumerated: hash ``home_city=Austin`` into one of a fixed
number of buckets and learn a weight for the bucket. The model then costs
``buckets x candidates`` however many cities there are, and the field becomes
evidence instead of being dropped.

**How it is fitted, and why not the textbook way.** A classifier is normally
trained with every feature present. Here the features are whichever boxes the
agent happened to fill first, so each record is presented several times during
training, each time with only a random handful of its fields visible, at the
same sizes the confidence calibration sweeps. Without that the model only ever
sees full rows and is asked about almost-empty ones.

**Where it is weak, stated plainly.**

*Nothing in it can be checked by hand.* ``home_city = Austin -> TX in 94% of
213 records`` is a sentence an agent can judge. "these three fields pulled
hardest toward TX" is not, and a negative weight - the real advantage of
fitting - has no plain-language form at all.

*One weight set serves every evidence size.* The value that makes two typed
fields come out right is not the value that makes one of them come out right
alone, and a single number has to do both. The masked presentation above helps
and does not fix it. The shared calibration is indexed by raw score and pools
the evidence sizes, so it corrects the average rather than the case.
"""

from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..features import MAX_DISTINCT, MIN_ROWS_FOR_SHARE, Profile
from . import relevance
from .base import Algorithm, Ballot, Engine, FitContext, Guess, register, tuning

# How many buckets a hashed field's values are folded into. The model's size
# stops depending on the field's cardinality and starts depending on this.
# Collisions cost accuracy, so this is the knob to raise on a form full of
# codes - at 4,096 a two-thousand-value field collides noticeably, at 65,536 it
# does not, and both are constant-sized.
BUCKETS = 4096

# Passes over the records, at most. Fitting stops early when passes stop
# helping, which is usually well before this.
EPOCHS = 12

# How many passes in a row may fail to help before fitting stops. One flat pass
# means little when every pass shows the records through a different mask.
PATIENCE = 2

LEARNING_RATE = 0.1

# Shrinkage toward zero. This is what stops a value seen twice from earning a
# confident weight - the job that leave-one-out and the permutation test do for
# a conditional table's lambda.
L2 = 2e-5

# Weight rows whose largest entry is under this are dropped when the model is
# written. They cannot change an answer and they are most of the file.
PRUNE = 0.05

# And then a hard ceiling per field, counted in numbers rather than rows,
# because a row costs one number per candidate answer. This is what keeps the
# model file in the same world as the other engines. Without it, nine bucketed
# fields against a field with 194 candidate values produced a 100 MB model on
# the claims form at 20,000 records. The rows with the largest weights are the
# ones kept, which makes the ceiling do double duty as feature selection: a
# bucket that learned nothing is the first thing dropped.
#
# Counting numbers rather than rows is deliberate, and it protects the case
# this engine exists for. Thousands of buckets predicting a field with five
# answers is cheap, and that is exactly city to state.
MAX_WEIGHTS = 40_000

# Bucketed sources offered to one target, at most. The shortlist ranks sources
# on a sample of a few hundred rows (:mod:`.relevance`), which is enough to
# judge a field with twenty values and not enough to judge one with four
# thousand buckets - every bucket looks informative when it holds one row. So
# the wide ones are rationed rather than trusted.
MAX_HASHED_SOURCES = 2

# Held back from the fit rows, per target, to stop on and to measure strength
# with. The run's own holdout is not touched: it belongs to the confidence
# curve, and an engine that peeked at it would make that curve a fiction.
CHECK_SHARE = 0.2
MAX_CHECK_ROWS = 400

# How many fields the training pretends the agent has typed. The same sweep the
# calibration uses, for the same reason: the model has to answer from one field
# as well as from eight.
MASKS = (1, 2, 3, 5, 8)

# Fields offered to each target, most relevant first. A shortlist is not a
# refinement here, it is what keeps the model honest: given every field, a
# linear model will happily fit weights to a column of pure noise and then
# answer confidently from it, and because the confidence curve is shared across
# every field, one overconfident field drags the whole curve down. The
# shortlist is :mod:`.relevance`'s cheap gain ratio, the same one naive Bayes
# and the neighbour search use, applied to the bucketed tokens so a wide field
# is judged as it will be used.
MAX_SOURCES = 10

# Beyond this, the linear score is clamped. exp of it is all that matters and
# the softmax has long since saturated.
LOGIT_LIMIT = 30.0


def bucket_of(field: str, value: str, buckets: int) -> str:
    """A stable bucket token for one value of a hashed field.

    ``crc32`` rather than ``hash``: the built-in is salted per process, so a
    model fitted in one run would answer differently in the next.
    """
    return f"{field}#{zlib.crc32(value.encode('utf-8')) % buckets}"


def hashable(profile: Profile) -> bool:
    """Whether an open field's values repeat enough to be worth hashing.

    A claim number is different in every record: bucketing it produces one
    bucket per row and learns nothing, at the cost of carrying the rows. A city
    with two thousand values over fifteen thousand records is the case this
    engine exists for. The test is the share-of-rows half of the profiler's own
    enumerable test - the half a high-cardinality field passes - so what gets
    hashed is exactly the fields that were dropped for being too wide and
    nothing else.
    """
    if profile.kind != "open" or profile.filled < MIN_ROWS_FOR_SHARE:
        return False
    return profile.distinct <= max(MIN_ROWS_FOR_SHARE, profile.filled * 0.5)


@dataclass
class Weights:
    """What was fitted for one target field."""

    classes: list[str]
    bias: list[float] = dc_field(default_factory=list)
    rows: dict[str, list[float]] = dc_field(default_factory=dict)
    #: Share of guessing errors removed over always answering the commonest
    #: value, measured on rows this target was not fitted on. The same quantity
    #: a conditional table reports as lambda, so the field report can mix them.
    strength: float = 0.0
    tested: int = 0
    epochs: int = 0
    #: How much of the fitted weight sits in each source field, normalised.
    #: What ``sources`` and ``reach`` are read off.
    importance: dict[str, float] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classes": list(self.classes),
            "bias": [round(b, 4) for b in self.bias],
            "rows": {token: [round(w, 4) for w in row]
                     for token, row in self.rows.items()},
            "strength": round(self.strength, 4),
            "tested": self.tested,
            "epochs": self.epochs,
            "importance": {k: round(v, 4) for k, v in self.importance.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Weights":
        return cls(
            classes=[str(v) for v in data.get("classes") or []],
            bias=[float(b) for b in data.get("bias") or []],
            rows={str(token): [float(w) for w in row]
                  for token, row in (data.get("rows") or {}).items()},
            strength=float(data.get("strength", 0.0)),
            tested=int(data.get("tested", 0)),
            epochs=int(data.get("epochs", 0)),
            importance={str(k): float(v)
                        for k, v in (data.get("importance") or {}).items()},
        )


class LinearEngine(Engine):
    """Per-target softmax over one-hot and hashed field values."""

    algorithm = "linear"

    def __init__(self, weights: dict[str, Weights],
                 hashed: list[str] | None = None,
                 buckets: int = BUCKETS) -> None:
        self.weights = weights
        #: Fields whose values are bucketed rather than named, because there
        #: are too many of them to enumerate.
        self.hashed = set(hashed or ())
        self.buckets = buckets

    # -- prediction --------------------------------------------------------

    def _token(self, source: str, value: str) -> str:
        if source in self.hashed:
            return bucket_of(source, value, self.buckets)
        return f"{source}={value}"

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        ballot = Ballot(self.combiner)
        fitted = self.weights.get(name)
        if fitted is None or not fitted.classes:
            ballot.floor(profile)
            return ballot.guess()

        count = len(fitted.classes)
        logits = list(fitted.bias) or [0.0] * count
        active: list[tuple[str, str, list[float]]] = []
        for source, value in known.items():
            if not value or source == name:
                continue
            row = fitted.rows.get(self._token(source, value))
            if row is None:
                continue
            for index in range(count):
                logits[index] += row[index]
            active.append((source, value, row))

        if not active:
            ballot.floor(profile)
            return ballot.guess()

        top = max(min(value, LOGIT_LIMIT) for value in logits)
        weights = [math.exp(max(-LOGIT_LIMIT, min(LOGIT_LIMIT, value)) - top)
                   for value in logits]
        mass = sum(weights)
        distribution = {fitted.classes[index]: weights[index] / mass
                        for index in range(count)}
        winner = max(range(count), key=lambda index: weights[index])
        ballot.cast(
            distribution, fitted.strength,
            reason=self._reason(active, winner, fitted.classes[winner],
                                distribution[fitted.classes[winner]]),
            strength=fitted.strength, support=fitted.tested,
        )
        ballot.floor(profile)
        return ballot.guess()

    def _reason(self, active: list[tuple[str, str, list[float]]], winner: int,
                answer: str, share: float) -> str:
        """Name the evidence that pulled hardest toward the answer.

        This is the honest best available, and it is weaker than what a
        conditional table says. A weight is not a count of anything, so there
        is no "in 94% of 213 records" to offer - only which fields pushed, and
        which way.
        """
        pulls = sorted(((row[winner], source, value) for source, value, row in active),
                       key=lambda item: -item[0])
        named = []
        for pull, source, value in pulls[:3]:
            if pull <= 0:
                break
            shown = f"{source} = {value}"
            if source in self.hashed:
                shown += " (bucketed)"
            named.append(shown)
        if not named:
            return (f"the weights favour {answer} at {share * 100:.0f}%, with nothing "
                    f"typed pushing toward it")
        return (f"{' and '.join(named)} -> {answer} at {share * 100:.0f}% "
                f"before calibration")

    # -- description -------------------------------------------------------

    def targets(self) -> list[str]:
        return list(self.weights)

    def sources(self, name: str) -> list[str]:
        fitted = self.weights.get(name)
        if fitted is None:
            return []
        return sorted(fitted.importance, key=lambda s: (-fitted.importance[s], s))

    def strength(self, name: str) -> float:
        fitted = self.weights.get(name)
        return fitted.strength if fitted else 0.0

    def reach(self, name: str, available: set[str]) -> float:
        """How much of the fitted evidence the caller could actually supply."""
        fitted = self.weights.get(name)
        if fitted is None or not fitted.importance:
            return 0.0
        covered = sum(share for source, share in fitted.importance.items()
                      if source in available)
        return fitted.strength * min(1.0, covered)

    def explain(self, name: str) -> str:
        sources = self.sources(name)
        if not sources:
            return ""
        shown = [f"{s} (bucketed)" if s in self.hashed else s for s in sources[:3]]
        return "weights on " + ", ".join(shown)

    def summary(self) -> dict[str, Any]:
        rows = sum(len(f.rows) for f in self.weights.values())
        numbers = sum(len(f.rows) * len(f.classes) for f in self.weights.values())
        strengths = [f.strength for f in self.weights.values()]
        return {
            "fields fitted": len(self.weights),
            "weight rows": rows,
            "weights": numbers,
            "buckets": self.buckets,
            "bucketed fields": len(self.hashed),
            "passes, most for one field": max((f.epochs for f in self.weights.values()),
                                              default=0),
            "strongest": round(max(strengths, default=0.0), 3),
        }

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "buckets": self.buckets,
            "hashed": sorted(self.hashed),
            "weights": {name: fitted.to_dict()
                        for name, fitted in self.weights.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinearEngine":
        return cls(
            weights={str(name): Weights.from_dict(entry)
                     for name, entry in (data.get("weights") or {}).items()},
            hashed=[str(name) for name in data.get("hashed") or []],
            buckets=int(data.get("buckets", BUCKETS)),
        )


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def _softmax(logits: list[float]) -> tuple[list[float], float]:
    """Probabilities, and the mass, from raw scores. Clamped, so it cannot overflow."""
    clamped = [max(-LOGIT_LIMIT, min(LOGIT_LIMIT, value)) for value in logits]
    top = max(clamped)
    weights = [math.exp(value - top) for value in clamped]
    return weights, sum(weights)


def _masked(pool: list[str], size: int, rng: random.Random) -> list[str]:
    return pool if size >= len(pool) else rng.sample(pool, size)


def _score(rows: list[int], pools: dict[int, list[str]], truths: dict[int, int],
           weight_rows: dict[str, list[float]], bias: list[float],
           count: int, full: bool = False) -> tuple[float, float]:
    """``(mean log loss, accuracy)`` on ``rows``, asked the way it will be asked.

    A fixed generator, so the masks are the same on every epoch and two epochs
    are actually comparable.

    ``full`` shows every field instead of a handful. Stopping reads the masked
    number, because that is the question an agent asks. The *strength* that
    goes into the field report reads the full one, because that is how
    :func:`.tree._score` measures a tree's, and a number two engines report
    under the same name has to mean the same thing.
    """
    rng = random.Random(17)
    total = 0.0
    attempts = hits = 0
    for row in rows:
        pool = pools[row]
        if not pool:
            continue
        truth = truths[row]
        for size in ((len(pool),) if full else MASKS):
            given = _masked(pool, size, rng)
            logits = list(bias)
            for token in given:
                weights = weight_rows.get(token)
                if weights is not None:
                    for index in range(count):
                        logits[index] += weights[index]
            probabilities, mass = _softmax(logits)
            attempts += 1
            total -= math.log(max(probabilities[truth] / mass, 1e-9))
            if max(range(count), key=lambda i: probabilities[i]) == truth:
                hits += 1
            if size >= len(pool):
                break
    if not attempts:
        return math.inf, 0.0
    return total / attempts, hits / attempts


def _keep_largest(weight_rows: dict[str, list[float]], count: int,
                  prune: float, ceiling: int) -> dict[str, list[float]]:
    """Drop the weights too small to matter, then the weakest over the ceiling."""
    scored = [(max(abs(value) for value in row), token, row)
              for token, row in weight_rows.items()]
    scored = [entry for entry in scored if entry[0] >= prune]
    allowed = max(1, ceiling // max(1, count))
    if len(scored) > allowed:
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        scored = scored[:allowed]
    return {token: row for _, token, row in scored}


def _offer(ranked: dict[str, float], target: str, hashed: list[str]) -> list[str]:
    """The sources one target may fit weights for, best first, wide ones rationed."""
    order = sorted(ranked, key=lambda source: (-ranked[source], source))
    chosen: list[str] = []
    wide = 0
    for source in order:
        if source == target:
            continue
        if source in hashed:
            if wide >= MAX_HASHED_SOURCES:
                continue
            wide += 1
        chosen.append(source)
    return chosen


def _fit_target(target: str, classes: list[str], columns: dict[str, list[str]],
                tokens: dict[str, list[str]], sources: list[str],
                epochs: int, rate: float, l2: float, prune: float,
                ceiling: int, rng: random.Random) -> Weights | None:
    """Fit one field's weights, stopping when another pass stops helping."""
    index = {value: position for position, value in enumerate(classes)}
    count = len(classes)
    target_values = columns[target]
    rows = [row for row, value in enumerate(target_values) if value in index]
    if len(rows) < 2 * len(MASKS):
        return None

    order = list(rows)
    rng.shuffle(order)
    cut = min(MAX_CHECK_ROWS, int(len(order) * CHECK_SHARE))
    check, learn = order[:cut], order[cut:]
    if not learn or not check:
        return None

    pools = {row: [tokens[source][row] for source in sources if tokens[source][row]]
             for row in order}
    truths = {row: index[target_values[row]] for row in order}

    weight_rows: dict[str, list[float]] = {}
    bias = [0.0] * count
    best: tuple[dict[str, list[float]], list[float]] | None = None
    best_loss = math.inf
    used = 0
    stale = 0

    for epoch in range(max(1, epochs)):
        rng.shuffle(learn)
        for row in learn:
            pool = pools[row]
            if not pool:
                continue
            truth = truths[row]
            for size in MASKS:
                active = []
                for token in _masked(pool, size, rng):
                    weights = weight_rows.get(token)
                    if weights is None:
                        weights = weight_rows[token] = [0.0] * count
                    active.append(weights)
                logits = list(bias)
                for weights in active:
                    for position in range(count):
                        logits[position] += weights[position]
                probabilities, mass = _softmax(logits)
                for position in range(count):
                    gradient = probabilities[position] / mass
                    if position == truth:
                        gradient -= 1.0
                    step = rate * gradient
                    bias[position] -= step
                    for weights in active:
                        weights[position] -= step + rate * l2 * weights[position]
                if size >= len(pool):
                    break

        loss, _ = _score(check, pools, truths, weight_rows, bias, count)
        if loss < best_loss - 1e-5:
            best_loss = loss
            best = ({token: list(row) for token, row in weight_rows.items()},
                    list(bias))
            used = epoch + 1
            stale = 0
        else:
            # One pass that does not help is not a reason to stop: the masked
            # presentation makes every pass a different question, so a flat one
            # happens. Two in a row means it is done, and the best pass is kept
            # rather than whichever one happened to be last.
            stale += 1
            if stale >= PATIENCE:
                break

    if best is None:
        return None
    weight_rows, bias = best

    # Cut down before it is measured, so the strength beside it describes the
    # model that actually gets written down rather than a fuller one.
    weight_rows = _keep_largest(weight_rows, count, prune, ceiling)
    _, accuracy = _score(check, pools, truths, weight_rows, bias, count, full=True)

    counts: dict[int, int] = {}
    for row in check:
        counts[truths[row]] = counts.get(truths[row], 0) + 1
    majority = max(counts.values()) / len(check) if check else 1.0
    strength = (0.0 if majority >= 1.0
                else max(0.0, (accuracy - majority) / (1.0 - majority)))

    importance: dict[str, float] = {}
    for token, row in weight_rows.items():
        source = token.split("#", 1)[0] if "#" in token else token.split("=", 1)[0]
        pull = max(abs(value) for value in row)
        if pull > importance.get(source, 0.0):
            importance[source] = pull
    total = sum(importance.values())
    if total > 0:
        importance = {k: v / total for k, v in importance.items()}

    return Weights(classes=classes, bias=bias, rows=weight_rows, strength=strength,
                   tested=len(check), epochs=used, importance=importance)


def fit(context: FitContext) -> LinearEngine:
    trace = context.trace
    columns = context.columns
    profiles = context.profiles
    buckets = max(64, tuning(context, "buckets", BUCKETS))
    epochs = max(1, tuning(context, "epochs", EPOCHS))
    rate = tuning(context, "learning_rate", LEARNING_RATE)
    l2 = tuning(context, "l2", L2)
    prune = tuning(context, "prune", PRUNE)
    ceiling = max(1000, tuning(context, "max_weights", MAX_WEIGHTS))
    seed = getattr(context.options, "seed", None)
    rng = random.Random(0 if seed is None else seed)

    usable = context.usable()
    hashed = [name for name, profile in profiles.items() if hashable(profile)]
    every = [name for name in usable] + hashed

    trace.step(
        f"fitting weights for {len(usable)} field(s) over {len(every)} source(s), "
        f"up to {epochs} pass(es) each, {len(MASKS)} evidence size(s) per record"
    )
    if hashed:
        trace.detail(
            f"  {len(hashed)} field(s) have too many values to enumerate and are "
            f"bucketed into {buckets}: {', '.join(hashed[:4])}"
            + (", ..." if len(hashed) > 4 else "")
        )
        trace.detail("  the conditional-table engine cannot see these at all")

    tokens: dict[str, list[str]] = {}
    for source in every:
        column = columns[source]
        if source in hashed:
            tokens[source] = [bucket_of(source, value, buckets) if value else ""
                              for value in column]
        else:
            tokens[source] = [f"{source}={value}" if value else ""
                              for value in column]

    shortlist = relevance.table(columns, usable, keep=MAX_SOURCES,
                                sources=every, source_columns=tokens)
    offered = sum(len(group) for group in shortlist.values())
    trace.detail(f"  {offered} field pair(s) worth fitting weights for, "
                 f"at most {MAX_SOURCES} per field")

    weights: dict[str, Weights] = {}
    for position, target in enumerate(usable, start=1):
        classes = [value for value, _ in profiles[target].values][:MAX_DISTINCT]
        if len(classes) < 2:
            # One value in every record. The marginal floor answers it exactly
            # and a softmax over one class has nothing to say.
            continue
        chosen = _offer(shortlist.get(target) or {}, target, hashed)
        if not chosen:
            continue
        if position % 6 == 0:
            trace.detail(f"  {position} of {len(usable)} field(s), on {target}")
        fitted = _fit_target(
            target, classes, columns, tokens, chosen,
            epochs, rate, l2, prune, ceiling, rng,
        )
        if fitted is not None and fitted.rows:
            weights[target] = fitted

    engine = LinearEngine(weights, hashed, buckets)
    rows = sum(len(f.rows) for f in weights.values())
    numbers = sum(len(f.rows) * len(f.classes) for f in weights.values())
    trace.done(f"fitted {len(weights)} field(s), {rows} weight row(s) and "
               f"{numbers} number(s) kept, at most {ceiling} per field")
    for target, fitted in sorted(weights.items(), key=lambda kv: -kv[1].strength)[:8]:
        best = engine.sources(target)[:2]
        trace.detail(f"  {target} <- {', '.join(best) or 'nothing'} "
                     f"(strength {fitted.strength:.2f}, {fitted.epochs} pass(es))")
    return engine


ALGORITHM = register(Algorithm(
    name="linear",
    label="Fitted weights (linear)",
    blurb="Learns a weight for every field value against every candidate answer, "
          "by gradient descent. The only engine that can use a field with "
          "thousands of distinct values as evidence, because it buckets them "
          "rather than listing them.",
    recipe=[
        "Profile each column, as every algorithm does.",
        "Take the fields with a small enough set of values as the ones to "
        "answer, and every field whose values repeat as evidence - including "
        "the wide ones the other engines drop.",
        "Turn each value into a token: named for a field that can be "
        "enumerated, hashed into a fixed number of buckets for a field that "
        "cannot.",
        "Give every token a weight per candidate answer, starting at zero.",
        "Show each record several times, each time hiding all but a random "
        "handful of its fields, because that is how an agent will ask.",
        "After each showing, push the weights of the fields that were visible "
        "toward the answer that was right and away from the ones that were "
        "not.",
        "Stop when another pass stops helping on records held back from the "
        "fit, and drop every weight too small to change an answer.",
        "To predict, add up the weights of the fields the agent has typed and "
        "turn the totals into probabilities.",
    ],
    fit=fit,
    load=LinearEngine.from_dict,
    knobs=[
        ("epochs", "Passes over the records, at most", "int", EPOCHS, 1, 40),
        ("learning_rate", "How far each step moves", "float", LEARNING_RATE, 0.01, 1.0),
        ("l2", "Pull toward zero", "float", L2, 0.0, 0.01),
        ("buckets", "Buckets for a field too wide to list", "int", BUCKETS, 256, 262144),
        ("prune", "Drop weights smaller than", "float", PRUNE, 0.0, 0.5),
        ("max_weights", "Weights kept per field, at most", "int",
         MAX_WEIGHTS, 1000, 2_000_000),
    ],
))
