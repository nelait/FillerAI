"""How loudly each voter should speak, measured rather than assumed.

Every engine here ends the same way: several opinions about one field, each
deserving more or less belief, averaged into something that still reads as a
probability. :class:`~.base.Ballot` does the averaging. What it has never done
is *know* how much each opinion deserves - the weights are hand-picked
constants and formulas:

* ``link.strength * support_weight(rows)`` for a conditional table
  (:mod:`.statistical`),
* ``tree.strength``, or a flat quarter of it when the tree could not ask
  anything (:mod:`.tree`),
* ``MARGINAL_WEIGHT = 0.2`` for what the field usually says,
* a fixed 0.5 when naive Bayes has nothing better to say about itself.

Each of those is a reasonable guess by someone who had thought about it. None
of them was ever fitted, and the signal needed to fit them is already being
computed and thrown away: the calibration pass predicts every field of every
held-out record and knows whether each answer was right.

So this is the smallest weight-based method in the project. Five features per
vote, six parameters, one logistic regression:

    weight = sigmoid(bias + w . (heuristic, strength, support, peak, floor))

fitted to predict *was this voter right*, which is exactly what "how much
should it be believed" means. The engine's own hand-picked weight is the first
feature rather than being replaced, so the fitted model starts from "believe
the heuristic" and adjusts from there.

**Two rules keep this honest, and both matter.**

*It is never fitted on the rows that calibrate.* A combiner tuned on the same
records the confidence curve is measured against would make that curve
describe rows the weights were chosen for, which is the one thing the holdout
exists to prevent. :func:`fillerai.train.model.train` takes a slice off the
front of the holdout for this and calibrates on the rest.

*It has to earn its place.* After fitting, the learned weights and the
hand-picked ones are both run over records neither was fitted on, and the
learned ones are kept only if they do at least as well. An unfitted or
rejected combiner returns the heuristic untouched, so the model behaves
exactly as it did before this module existed - bit for bit.
"""

from __future__ import annotations

import math
import random
from contextlib import contextmanager
from typing import Any, Iterable

#: What the combiner is told about one vote, in order. Every one of these is
#: known at predict time on the machine doing the predicting - a combiner that
#: needed the answer would be no use.
FEATURES = ("heuristic", "strength", "support", "peak", "floor")

# Mirrors ``associate.SUPPORT_SMOOTHING``, kept here so this module imports
# nothing: a bucket of 5 rows is worth half of a bucket of many.
SUPPORT_SMOOTHING = 5.0

# Believe the heuristic, softly, before anything has been measured. A fitted
# combiner starts here rather than at "every voter is equal", which is a worse
# starting point than the guess it is trying to improve on.
INITIAL_BIAS = -2.0
INITIAL_HEURISTIC = 4.0

# Votes kept to fit on. The curve is six parameters and settles long before
# this; a 20,000-record run offers millions.
MAX_VOTES = 200_000

# The linear score is turned into odds, so it is clamped rather than squashed.
# At 8 that is a 3,000:1 voter against a 1:3,000 one, which is already further
# apart than any real pair of predictors.
ODDS_LIMIT = 8.0

LEARNING_RATE = 0.08
PASSES = 6
# Shrinkage, so a feature that happens to line up on a thin sample does not
# run away with the vote.
L2 = 1e-4


def support_share(rows: int) -> float:
    """How much a bucket of ``rows`` observations deserves, saturating at 1."""
    return rows / (rows + SUPPORT_SMOOTHING) if rows > 0 else 0.0


# ----------------------------------------------------------------------
# collecting votes
# ----------------------------------------------------------------------

#: While true, every ballot carries its individual votes out with the guess so
#: they can be scored against the truth. Off everywhere else, because a
#: prediction has no use for them and allocating them would be waste.
RECORD = False


@contextmanager
def recording():
    """Make ballots report their individual votes, for the duration."""
    global RECORD
    previous = RECORD
    RECORD = True
    try:
        yield
    finally:
        RECORD = previous


# ----------------------------------------------------------------------
# the combiner
# ----------------------------------------------------------------------


class Combiner:
    """How much a vote deserves to be believed.

    Unfitted, it answers with the weight the engine proposed, which is what
    every engine did before this existed.
    """

    def __init__(self, weights: list[float] | None = None, bias: float = 0.0,
                 fitted: bool = False, votes: int = 0,
                 loss: float = 0.0, baseline_loss: float = 0.0,
                 accuracy: float = 0.0, baseline_accuracy: float = 0.0) -> None:
        self.weights = list(weights) if weights else [0.0] * len(FEATURES)
        self.bias = bias
        self.fitted = fitted
        #: How many votes it was fitted on, for the summary.
        self.votes = votes
        #: What it scored on records neither set of weights was fitted on, and
        #: what the hand-picked weights scored on the same records. Kept
        #: because a learned weight nobody can check needs a number beside it.
        #:
        #: The gate is the loss, not the accuracy, and the reason is worth
        #: knowing: a ballot with one voter returns that voter's distribution
        #: whatever weight it was given, because the result is normalised by
        #: the weight cast. So the weights only ever move a ballot where
        #: voters compete, and what they move first is the *order and spacing*
        #: of the scores rather than which candidate wins. Log loss sees that;
        #: top-1 accuracy mostly does not. And score spacing is what the
        #: calibration curve above turns into the confidence a user reads, so
        #: it is the thing worth improving.
        self.loss = loss
        self.baseline_loss = baseline_loss
        self.accuracy = accuracy
        self.baseline_accuracy = baseline_accuracy

    # -- use ---------------------------------------------------------------

    def weight(self, features: tuple[float, ...]) -> float:
        """How loudly this vote speaks - its odds of being right.

        The fit predicts a probability, but a probability is the wrong thing to
        hand a weighted average. Two voters at 0.99 and 0.97 are three times
        apart in how often they are wrong and 2% apart in how loudly they would
        speak, and a logistic model on features this strong saturates near the
        top, so the differences that matter get squashed out. The odds do not
        squash: ``exp`` of the same linear score, which is ``p / (1 - p)``. A
        voter twice as likely to be right as wrong speaks twice as loudly as
        one at even odds.

        Nothing runs away with the ballot, because :meth:`Ballot.result`
        divides by the weight cast. An overwhelming voter simply gets its own
        distribution back, which is the honest answer when one voter really is
        overwhelming.
        """
        if not self.fitted:
            return features[0]
        total = self.bias
        for value, weight in zip(features, self.weights):
            total += value * weight
        return math.exp(max(-ODDS_LIMIT, min(ODDS_LIMIT, total)))

    # -- description -------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        if not self.fitted:
            if not self.votes:
                return {}
            return {
                "vote weights": "measured and declined",
                "loss": round(self.loss, 4),
                "loss with the hand-picked weights": round(self.baseline_loss, 4),
            }
        out: dict[str, Any] = {
            "votes fitted on": self.votes,
            "loss": round(self.loss, 4),
            "loss with the hand-picked weights": round(self.baseline_loss, 4),
            "accuracy": round(self.accuracy, 4),
            "accuracy with the hand-picked weights": round(self.baseline_accuracy, 4),
        }
        for name, weight in zip(FEATURES, self.weights):
            out[name] = round(weight, 3)
        return out

    def explain(self) -> list[str]:
        """One line per feature, largest pull first, for a log or a report."""
        if not self.fitted:
            if self.votes:
                return [f"fitted vote weights were declined: loss "
                        f"{self.loss:.4f} against {self.baseline_loss:.4f} "
                        f"hand-picked, so the hand-picked ones stay"]
            return ["vote weights are the hand-picked ones"]
        order = sorted(zip(FEATURES, self.weights), key=lambda kv: -abs(kv[1]))
        lines = [f"fitted on {self.votes} vote(s); loss {self.loss:.4f} against "
                 f"{self.baseline_loss:.4f} hand-picked, accuracy "
                 f"{self.accuracy * 100:.1f}% against "
                 f"{self.baseline_accuracy * 100:.1f}%"]
        lines += [f"  {name}: {weight:+.2f}" for name, weight in order]
        return lines

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitted": self.fitted,
            "features": list(FEATURES),
            "weights": [round(w, 6) for w in self.weights],
            "bias": round(self.bias, 6),
            "votes": self.votes,
            "loss": round(self.loss, 6),
            "baseline_loss": round(self.baseline_loss, 6),
            "accuracy": round(self.accuracy, 6),
            "baseline_accuracy": round(self.baseline_accuracy, 6),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Combiner":
        """Rebuild one, tolerating a model written before it existed.

        A saved combiner naming features this build does not have is not
        loaded at all: the weights would land on the wrong columns, and
        falling back on the hand-picked ones is both safe and correct.
        """
        if not data:
            return cls()
        measured = {
            "votes": int(data.get("votes", 0)),
            "loss": float(data.get("loss", 0.0)),
            "baseline_loss": float(data.get("baseline_loss", 0.0)),
            "accuracy": float(data.get("accuracy", 0.0)),
            "baseline_accuracy": float(data.get("baseline_accuracy", 0.0)),
        }
        if not data.get("fitted"):
            return cls(**measured)
        if list(data.get("features") or FEATURES) != list(FEATURES):
            return cls(**measured)
        weights = [float(w) for w in (data.get("weights") or [])]
        if len(weights) != len(FEATURES):
            return cls(**measured)
        return cls(weights=weights, bias=float(data.get("bias", 0.0)),
                   fitted=True, **measured)


#: What an engine uses until told otherwise, and what a rejected fit leaves
#: behind. Shared and immutable in practice - nothing mutates a Combiner.
HEURISTIC = Combiner()


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def fit(votes: Iterable[tuple[tuple[float, ...], bool]],
        seed: int | None = None) -> Combiner:
    """Fit weights from ``(features, was_right)`` pairs.

    Plain logistic regression by stochastic gradient descent. There is no
    early stopping and no validation split in here on purpose: six parameters
    on tens of thousands of votes do not overfit, and the decision about
    whether to use the result at all is made outside, on records this never
    saw.
    """
    sample = list(votes)
    if len(sample) > MAX_VOTES:
        step = len(sample) / MAX_VOTES
        sample = [sample[int(index * step)] for index in range(MAX_VOTES)]
    if len(sample) < 50:
        return Combiner()

    weights = [0.0] * len(FEATURES)
    weights[0] = INITIAL_HEURISTIC
    bias = INITIAL_BIAS
    rng = random.Random(0 if seed is None else seed)
    order = list(range(len(sample)))

    for _ in range(PASSES):
        rng.shuffle(order)
        for index in order:
            features, right = sample[index]
            total = bias
            for value, weight in zip(features, weights):
                total += value * weight
            total = max(-30.0, min(30.0, total))
            predicted = 1.0 / (1.0 + math.exp(-total))
            error = predicted - (1.0 if right else 0.0)
            bias -= LEARNING_RATE * error
            for position, value in enumerate(features):
                weights[position] -= LEARNING_RATE * (
                    error * value + L2 * weights[position])

    return Combiner(weights=weights, bias=bias, fitted=True, votes=len(sample))
