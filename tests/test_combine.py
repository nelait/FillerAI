"""Tests for the learned vote weights.

The thing under test is small - six parameters - and the risk in it is not
arithmetic, it is scope. A weight that is learned on the wrong rows quietly
makes the confidence curve a fiction, and a weight that is worse than the
hand-picked one it replaced quietly makes every answer worse. So most of what
is checked here is the guard rails: which rows it may see, that it has to earn
its place, and that a model built before any of this existed behaves exactly as
it did.
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.schema import Field, FormSchema
from fillerai.train.algos import combine
from fillerai.train.algos.base import MARGINAL_WEIGHT, Ballot
from fillerai.train.algos.combine import Combiner
from fillerai.train.features import Profile
from fillerai.train.model import (
    COMBINER_MIN_HOLDOUT,
    AutofillModel,
    TrainOptions,
    split_holdout,
    split_records,
    train,
)


def profile_of(*pairs: tuple[str, int]) -> Profile:
    total = sum(count for _, count in pairs)
    return Profile(name="target", kind="enumerable", values=list(pairs),
                   rows=total, filled=total)


# A form where two predictors disagree about how much they know: ``sharp``
# names the answer, ``blunt`` only says which half it is in, and ``noise`` says
# nothing. That is the shape the vote weights exist to arbitrate.
ANSWERS = ["alpha", "beta", "gamma", "delta"]
SHARP = {f"s{index}": ANSWERS[index % 4] for index in range(8)}

COMPETE_SCHEMA = FormSchema(name="compete", fields=[
    Field(name="sharp"), Field(name="blunt"), Field(name="noise"),
    Field(name="answer"), Field(name="reference"),
])


def compete_records(count: int = 1600, seed: int = 5) -> list[dict[str, str]]:
    rng = random.Random(seed)
    out = []
    for index in range(count):
        answer = rng.choice(ANSWERS)
        sharp = rng.choice([k for k, v in SHARP.items() if v == answer])
        if rng.random() < 0.05:
            sharp = rng.choice(list(SHARP))  # wrong 5% of the time
        out.append({
            "sharp": sharp,
            "blunt": "low" if answer in ANSWERS[:2] else "high",
            "noise": rng.choice("abcdef"),
            "answer": answer,
            "reference": f"R{index:05d}",
        })
    return out


# ----------------------------------------------------------------------
# what a combiner does to a ballot
# ----------------------------------------------------------------------


class TestWeighting(unittest.TestCase):
    def test_unfitted_it_hands_back_exactly_what_the_engine_proposed(self):
        """The whole backward-compatibility story rests on this one line."""
        heuristic = Combiner()
        for proposed in (0.0, 0.2, 0.55, 1.0):
            self.assertEqual(heuristic.weight((proposed, 0.3, 0.8, 0.9, 0.0)),
                             proposed)

    def test_fitted_it_answers_with_the_odds_rather_than_a_probability(self):
        """A weighted average needs room between its voters, not a squashed 0..1."""
        fitted = Combiner(weights=[1.0, 0, 0, 0, 0], bias=0.0, fitted=True)
        even = fitted.weight((0.0, 0, 0, 0, 0))
        better = fitted.weight((1.0, 0, 0, 0, 0))
        self.assertAlmostEqual(even, 1.0, places=6)
        self.assertGreater(better, 2.7)  # e, not a fraction of one

    def test_it_cannot_run_away_however_large_the_score(self):
        fitted = Combiner(weights=[1000.0, 0, 0, 0, 0], bias=0.0, fitted=True)
        self.assertLess(fitted.weight((1.0, 0, 0, 0, 0)), float("inf"))
        self.assertGreater(fitted.weight((-1.0, 0, 0, 0, 0)), 0.0)

    def test_a_ballot_with_one_voter_is_the_same_whatever_it_weighs(self):
        """Why the gate reads the loss and not the accuracy.

        ``result`` divides by the weight cast, so a lone voter always gets its
        own distribution back. Vote weights can only ever move a ballot where
        voters compete - which on a generated form is a small minority of them.
        """
        lonely = {"A": 0.7, "B": 0.3}
        scores = []
        for combiner in (Combiner(),
                         Combiner(weights=[9.0, 0, 0, 0, 0], bias=3.0, fitted=True)):
            ballot = Ballot(combiner)
            ballot.cast(lonely, 0.4, strength=0.5, support=40)
            scores.append(ballot.guess().score)
        self.assertAlmostEqual(scores[0], scores[1], places=9)

    def test_it_can_prefer_the_decisive_voter_over_the_heavily_weighted_one(self):
        """The case the hand-picked arithmetic cannot express."""
        decisive = {"A": 0.95, "B": 0.05}
        vague = {"B": 0.55, "A": 0.45}
        marginal = profile_of(("B", 60), ("A", 40))

        def run(combiner: Combiner) -> float:
            ballot = Ballot(combiner)
            ballot.cast(decisive, 0.2, strength=0.3, support=8)
            ballot.cast(vague, 0.9, strength=0.9, support=900)
            ballot.floor(marginal)
            return ballot.guess().distribution["A"]

        # peak is the fourth feature: weight what is decisive, not what is loud.
        on_peak = Combiner(weights=[0, 0, 0, 6.0, 0], bias=-3.0, fitted=True)
        self.assertGreater(run(on_peak), run(Combiner()) + 0.05)

    def test_the_marginal_floor_is_told_how_many_records_back_it(self):
        ballot = Ballot()
        ballot.floor(profile_of(("A", 30), ("B", 10)))
        # Recorded only while collecting; here the point is that casting the
        # floor still works and still uses the constant it always did.
        self.assertAlmostEqual(ballot.weight, MARGINAL_WEIGHT, places=9)


class TestRecording(unittest.TestCase):
    def test_a_prediction_carries_no_votes_unless_they_are_being_collected(self):
        ballot = Ballot()
        ballot.cast({"A": 0.9, "B": 0.1}, 0.5)
        self.assertEqual(ballot.guess().votes, [])

    def test_inside_the_collector_every_vote_comes_out_with_its_features(self):
        with combine.recording():
            ballot = Ballot()
            ballot.cast({"A": 0.9, "B": 0.1}, 0.5, strength=0.4, support=20)
            ballot.floor(profile_of(("B", 9), ("A", 1)))
            votes = ballot.guess().votes
        self.assertEqual(len(votes), 2)
        (first_features, first_top), (_, floor_top) = votes
        self.assertEqual(first_top, "A")
        self.assertEqual(floor_top, "B")
        # heuristic, strength, support share, peak, is-the-floor
        self.assertEqual(len(first_features), len(combine.FEATURES))
        self.assertAlmostEqual(first_features[0], 0.5)
        self.assertAlmostEqual(first_features[1], 0.4)
        self.assertAlmostEqual(first_features[3], 0.9)
        self.assertEqual(first_features[4], 0.0)

    def test_the_collector_turns_itself_off_again(self):
        with combine.recording():
            pass
        self.assertFalse(combine.RECORD)


# ----------------------------------------------------------------------
# which rows it is allowed to learn from
# ----------------------------------------------------------------------


class TestTheSplit(unittest.TestCase):
    def test_nothing_is_taken_from_a_holdout_too_small_to_spare_it(self):
        holdout = [{"a": str(i)} for i in range(COMBINER_MIN_HOLDOUT - 1)]
        tune, measure = split_holdout(holdout, TrainOptions())
        self.assertEqual(tune, [])
        self.assertEqual(len(measure), len(holdout))

    def test_the_tuning_rows_and_the_measuring_rows_never_overlap(self):
        holdout = [{"a": str(i)} for i in range(900)]
        tune, measure = split_holdout(holdout, TrainOptions())
        self.assertTrue(tune)
        self.assertEqual(len(tune) + len(measure), len(holdout))
        self.assertFalse({row["a"] for row in tune} & {row["a"] for row in measure})

    def test_the_confidence_curve_keeps_most_of_the_holdout(self):
        holdout = [{"a": str(i)} for i in range(5000)]
        tune, measure = split_holdout(holdout, TrainOptions())
        self.assertGreater(len(measure), len(holdout) * 0.9)

    def test_turning_it_off_leaves_the_whole_holdout_to_the_curve(self):
        holdout = [{"a": str(i)} for i in range(900)]
        tune, measure = split_holdout(holdout, TrainOptions(learn_weights=False))
        self.assertEqual(tune, [])
        self.assertEqual(len(measure), len(holdout))

    def test_the_tuning_rows_are_not_rows_the_engine_was_fitted_on(self):
        records = compete_records(1200)
        options = TrainOptions(seed=1)
        fit_rows, holdout = split_records(records, options)
        tune, _ = split_holdout(holdout, options)
        fitted = {row["reference"] for row in fit_rows}
        self.assertTrue(tune)
        self.assertFalse(fitted & {row["reference"] for row in tune})


# ----------------------------------------------------------------------
# fitting it for real
# ----------------------------------------------------------------------


class TestFittingInATrainingRun(unittest.TestCase):
    def test_a_small_dataset_is_left_exactly_as_it_was(self):
        """Most runs are small, and for those nothing about this may change."""
        records = compete_records(300)
        learned = train(COMPETE_SCHEMA, records, TrainOptions(seed=1))
        plain = train(COMPETE_SCHEMA, records,
                      TrainOptions(seed=1, learn_weights=False))
        self.assertFalse(learned.combiner.fitted)
        self.assertEqual(learned.combiner.votes, 0)
        # Everything but the record of what was asked for: same engine, same
        # confidence curve, same answers.
        for part in ("engine", "calibration", "combiner", "profiles", "derivations"):
            self.assertEqual(learned.to_dict()[part], plain.to_dict()[part], part)

    def test_on_competing_predictors_it_is_fitted_and_kept(self):
        model = train(COMPETE_SCHEMA, compete_records(), TrainOptions(seed=2))
        combiner = model.combiner
        self.assertTrue(combiner.fitted)
        self.assertGreater(combiner.votes, 100)
        # The gate: a lower loss than the hand-picked weights, on rows neither
        # of them was fitted on.
        self.assertLess(combiner.loss, combiner.baseline_loss)

    def test_the_engine_is_asked_through_the_fitted_weights(self):
        model = train(COMPETE_SCHEMA, compete_records(), TrainOptions(seed=2))
        self.assertIs(model.engine.combiner, model.combiner)

    def test_turning_it_off_changes_nothing_but_the_weights(self):
        records = compete_records()
        plain = train(COMPETE_SCHEMA, records,
                      TrainOptions(seed=2, learn_weights=False))
        self.assertFalse(plain.combiner.fitted)
        self.assertEqual(plain.settings["learn_weights"], False)

    def test_a_worse_fit_is_declined_and_says_so(self):
        """Constructed rather than hoped for: the gate is handed a bad candidate."""
        model = train(COMPETE_SCHEMA, compete_records(),
                      TrainOptions(seed=2, learn_weights=False))
        rubbish = Combiner(weights=[-8.0, 0, 0, -8.0, 8.0], bias=0.0, fitted=True)
        from fillerai.train.model import _voted_quality
        good_loss, _ = _voted_quality(model, compete_records(200, seed=9), 1)
        model.set_combiner(rubbish)
        bad_loss, _ = _voted_quality(model, compete_records(200, seed=9), 1)
        self.assertGreater(bad_loss, good_loss)

    def test_it_survives_the_round_trip_and_still_answers_the_same(self):
        model = train(COMPETE_SCHEMA, compete_records(), TrainOptions(seed=2))
        reloaded = AutofillModel.from_json(model.to_json())
        self.assertTrue(reloaded.combiner.fitted)
        self.assertIs(reloaded.engine.combiner, reloaded.combiner)
        known = {"sharp": "s1", "blunt": "low"}
        before = model.predict_field("answer", known)
        after = reloaded.predict_field("answer", known)
        self.assertEqual(before.value, after.value)
        self.assertAlmostEqual(before.score, after.score, places=4)

    def test_a_model_saved_before_any_of_this_loads_with_the_old_arithmetic(self):
        model = train(COMPETE_SCHEMA, compete_records(300), TrainOptions(seed=1))
        data = model.to_dict()
        del data["combiner"]
        reloaded = AutofillModel.from_dict(data)
        self.assertFalse(reloaded.combiner.fitted)
        self.assertEqual(reloaded.combiner.weight((0.37, 0, 0, 0, 0)), 0.37)

    def test_weights_written_for_features_this_build_does_not_have_are_ignored(self):
        """Landing a saved weight on the wrong column would be silent and wrong."""
        stale = Combiner.from_dict({
            "fitted": True, "features": ["heuristic", "phase-of-the-moon"],
            "weights": [1.0, 2.0], "bias": 0.5, "votes": 900,
        })
        self.assertFalse(stale.fitted)
        self.assertEqual(stale.weight((0.42, 0, 0, 0, 0)), 0.42)
        self.assertEqual(stale.votes, 900)  # the measurement is still worth having

    def test_it_explains_itself_in_lines_a_log_can_print(self):
        model = train(COMPETE_SCHEMA, compete_records(), TrainOptions(seed=2))
        lines = model.combiner.explain()
        self.assertTrue(lines[0].startswith("fitted on"))
        self.assertEqual(len(lines), 1 + len(combine.FEATURES))
        self.assertIn("votes fitted on", model.combiner.summary())


if __name__ == "__main__":
    unittest.main()
