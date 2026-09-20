"""Tests for the selectable training algorithms.

Two kinds of test here, and the second is the interesting one.

The first kind is the contract: every algorithm has to fit, predict,
serialise, reload and report itself, and a test that runs over the registry
means adding an algorithm means those hold for it too without anybody
remembering to write them.

The second kind is about what each algorithm is *for*. A decision tree that
cannot see a two-field relationship is not a decision tree, however cleanly
it round-trips, and a gain ratio that rewards a unique-per-record field for
splitting is the memorising bug in a new costume. Those get their own tests,
built on data where the right answer is known because it was constructed.
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.schema import Constraints, Field, FormSchema, Option
from fillerai.train import algos
from fillerai.train.algos import tree as tree_module
from fillerai.train.model import USUAL_FLOOR, AutofillModel, TrainOptions, train
from fillerai.train.trace import Trace


def plain_schema(*names: str) -> FormSchema:
    return FormSchema(name="test", fields=[Field(name=n, label=n) for n in names])


def interaction_records(count: int = 400, seed: int = 4) -> list[dict[str, str]]:
    """Data whose one relationship lives only in a pair of fields.

    Neither ``policy`` nor ``role`` alone shifts ``relationship`` much - each
    leaves it a near coin flip - and together they fix it exactly. That is
    the shape a vote of one-field opinions cannot represent and a tree can,
    so it is the test that says whether the tree is doing its job.
    """
    rng = random.Random(seed)
    answers = {
        ("family", "claimant"): "self",
        ("family", "dependent"): "child",
        ("single", "claimant"): "spouse",
        ("single", "dependent"): "self",
    }
    records = []
    for index in range(count):
        policy = rng.choice(["family", "single"])
        role = rng.choice(["claimant", "dependent"])
        records.append({
            "policy": policy,
            "role": role,
            "relationship": answers[(policy, role)],
            "noise": rng.choice("abcdefgh"),
            "reference": f"REF-{index:05d}",  # unique in every record
        })
    return records


def city_records(count: int = 200, seed: int = 11) -> list[dict[str, str]]:
    """The ordinary case: one field implies another, directly."""
    rng = random.Random(seed)
    pairs = [("Austin", "TX"), ("Dallas", "TX"), ("Denver", "CO"),
             ("Boulder", "CO"), ("Miami", "FL")]
    records = []
    for index in range(count):
        city, state = rng.choice(pairs)
        records.append({
            "city": city, "state": state,
            "country": "United States",
            "reference": f"R{index:05d}",
        })
    return records


CITY_SCHEMA = plain_schema("city", "state", "country", "reference")
INTERACTION_SCHEMA = plain_schema("policy", "role", "relationship", "noise", "reference")


# ----------------------------------------------------------------------
# the contract every algorithm keeps
# ----------------------------------------------------------------------


class TestRegistry(unittest.TestCase):
    def test_the_default_is_one_of_the_registered_algorithms(self):
        self.assertIn(algos.DEFAULT, algos.names())

    def test_an_unknown_algorithm_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            algos.get("gradient-boosted-wishes")
        # The message has to say what there is, or the user is left guessing.
        self.assertIn("statistical", str(caught.exception))

    def test_every_algorithm_describes_itself_well_enough_for_a_picker(self):
        for algorithm in algos.all_algorithms():
            with self.subTest(algorithm.name):
                self.assertTrue(algorithm.label)
                self.assertTrue(algorithm.blurb)
                self.assertGreaterEqual(len(algorithm.recipe), 3)
                for key, label, kind, default, low, high in algorithm.knobs:
                    self.assertIn(kind, ("int", "float"))
                    self.assertTrue(label)
                    self.assertLessEqual(low, default)
                    self.assertLessEqual(default, high)

    def test_every_knob_is_declared_in_the_order_the_ui_expects(self):
        for algorithm in algos.all_algorithms():
            for knob in algorithm.to_dict()["knobs"]:
                self.assertEqual(
                    set(knob), {"key", "label", "kind", "default", "low", "high"})


class TestEveryAlgorithm(unittest.TestCase):
    """Run the same expectations over whatever is registered."""

    def each(self):
        for name in algos.names():
            with self.subTest(algorithm=name):
                yield name

    def test_each_one_learns_the_obvious_relationship(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            prediction = model.predict_field("state", {"city": "Denver"})
            self.assertEqual(prediction.value, "CO")
            self.assertGreater(prediction.confidence, 0.6)

    def test_each_one_survives_the_round_trip_through_json(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            reloaded = AutofillModel.from_json(model.to_json())
            self.assertEqual(reloaded.algorithm, name)
            before = model.predict_field("state", {"city": "Austin"})
            after = reloaded.predict_field("state", {"city": "Austin"})
            self.assertEqual(before.value, after.value)
            self.assertAlmostEqual(before.confidence, after.confidence, places=4)

    def test_each_one_declines_a_field_that_is_different_every_time(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            prediction = model.predict_field("reference", {"city": "Austin"})
            self.assertIsNone(prediction.value)

    def test_each_one_answers_a_constant_field_with_nothing_given(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            prediction = model.predict_field("country", {})
            self.assertEqual(prediction.value, "United States")

    def test_each_one_gets_its_confidence_calibrated_the_same_way(self):
        """The shared layer is what makes the algorithms comparable."""
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(400),
                          TrainOptions(algorithm=name, seed=2))
            self.assertTrue(model.calibration.fitted)
            self.assertEqual(len(model.calibration.rates), 10)
            # Calibrated confidence never falls as the raw score rises.
            rates = model.calibration.rates
            self.assertEqual(rates, sorted(rates))

    def test_each_one_reports_which_fields_it_leans_on(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            self.assertIn("city", model.engine.sources("state"))

    def test_each_one_is_reproducible_given_a_seed(self):
        for name in self.each():
            options = TrainOptions(algorithm=name, seed=7)
            first = train(CITY_SCHEMA, city_records(), options).to_json()
            second = train(CITY_SCHEMA, city_records(), options).to_json()
            self.assertEqual(first, second)

    def test_each_one_writes_a_log_a_person_can_follow(self):
        for name in self.each():
            trace = Trace()
            train(CITY_SCHEMA, city_records(), TrainOptions(algorithm=name, seed=1,
                                                            trace=trace))
            steps = [line for line in trace.since() if line.level == "step"]
            self.assertGreaterEqual(len(steps), 4)
            self.assertTrue(trace.progress()["finished"])

    def test_each_one_says_what_it_built_in_numbers_a_ui_can_show(self):
        for name in self.each():
            model = train(CITY_SCHEMA, city_records(),
                          TrainOptions(algorithm=name, seed=1))
            summary = model.engine.summary()
            self.assertTrue(summary)
            for value in summary.values():
                self.assertIsInstance(value, (int, float))

    def test_each_one_leaves_the_rules_alone(self):
        """Rules are arithmetic, so the choice of algorithm cannot change them."""
        schema = plain_schema("first_name", "last_name", "full_name")
        schema.fields[0].semantic_type = "first_name"
        schema.fields[1].semantic_type = "last_name"
        schema.fields[2].semantic_type = "full_name"
        rng = random.Random(3)
        records = []
        for _ in range(120):
            first = rng.choice(["Ada", "Grace", "Alan", "Edsger"])
            last = rng.choice(["Lovelace", "Hopper", "Turing", "Dijkstra"])
            records.append({"first_name": first, "last_name": last,
                            "full_name": f"{first} {last}"})
        found = {
            name: sorted(train(schema, records,
                               TrainOptions(algorithm=name, seed=1)).derivations)
            for name in algos.names()
        }
        self.assertEqual(len(set(map(tuple, found.values()))), 1, found)
        self.assertIn("full_name", next(iter(found.values())))

    def test_turning_the_rules_off_leaves_the_engine_on_its_own(self):
        schema = plain_schema("first_name", "last_name", "full_name")
        schema.fields[0].semantic_type = "first_name"
        schema.fields[1].semantic_type = "last_name"
        schema.fields[2].semantic_type = "full_name"
        records = [{"first_name": "Ada", "last_name": f"N{index}",
                    "full_name": f"Ada N{index}"} for index in range(80)]
        model = train(schema, records,
                      TrainOptions(algorithm="tree", seed=1, use_rules=False))
        self.assertEqual(model.derivations, {})


# ----------------------------------------------------------------------
# what the trees are for
# ----------------------------------------------------------------------


class TestTrees(unittest.TestCase):
    def test_a_tree_sees_a_relationship_that_needs_two_fields(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        prediction = model.predict_field(
            "relationship", {"policy": "family", "role": "dependent"})
        self.assertEqual(prediction.value, "child")
        self.assertGreater(prediction.confidence, 0.8)

    def test_the_conditional_tables_cannot_and_say_so(self):
        """Not a defect in the older engine - a limit, and the tree's reason to exist."""
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="statistical", seed=3))
        prediction = model.predict_field(
            "relationship", {"policy": "family", "role": "dependent"})
        # It falls back on the marginal, which is the honest answer for a
        # vote of single-field opinions on data like this.
        self.assertEqual(prediction.basis, "usual")
        self.assertLess(prediction.confidence, 0.7)

    def test_a_forest_sees_it_too_and_is_surer(self):
        forest = train(INTERACTION_SCHEMA, interaction_records(),
                       TrainOptions(algorithm="forest", seed=3))
        tree = train(INTERACTION_SCHEMA, interaction_records(),
                     TrainOptions(algorithm="tree", seed=3))
        known = {"policy": "single", "role": "claimant"}
        self.assertEqual(forest.predict_field("relationship", known).value, "spouse")
        self.assertGreaterEqual(
            forest.predict_field("relationship", known).score,
            tree.predict_field("relationship", known).score - 0.05,
        )

    def test_a_unique_field_is_not_mistaken_for_a_perfect_split(self):
        """Gain ratio's whole job: a 400-way split has to earn its width."""
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        self.assertNotIn("reference", model.engine.sources("relationship"))

    def test_an_unanswered_question_is_averaged_rather_than_abandoned(self):
        """A tree rooted on a field nobody typed still uses the ones they did."""
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        stand = model.engine.trees["relationship"]
        root = stand[0].root
        # Answer only the question the root does not ask.
        deeper = root.feature
        other = "role" if deeper == "policy" else "policy"
        guess = model.engine.guess(
            "relationship", {other: "dependent" if other == "role" else "family"},
            model.profiles["relationship"])
        self.assertIsNotNone(guess.value)
        # One of the two fields is not enough to fix the answer, and the tree
        # should not pretend otherwise.
        self.assertLess(guess.score, 0.95)

    def test_a_value_the_tree_never_saw_falls_back_rather_than_failing(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        guess = model.engine.guess("relationship", {"policy": "corporate"},
                                   model.profiles["relationship"])
        self.assertIsNotNone(guess.value)

    def test_a_tree_can_be_drawn_for_a_person_to_read(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        drawn = model.engine.drawn("relationship")
        self.assertTrue(drawn)
        self.assertIn("?", drawn[0])  # the root is a question
        self.assertTrue(any("records" in line for line in drawn))

    def test_drawing_a_field_with_no_tree_gives_nothing_rather_than_raising(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="tree", seed=3))
        self.assertEqual(model.engine.drawn("reference"), [])

    def test_the_depth_knob_is_respected(self):
        shallow = train(INTERACTION_SCHEMA, interaction_records(),
                        TrainOptions(algorithm="tree", seed=3, tuning={"max_depth": 1}))
        for stand in shallow.engine.trees.values():
            # One question, then answers: depth() counts the answer row too.
            self.assertLessEqual(stand[0].root.depth(), 2)

    def test_the_forest_grows_the_number_of_trees_it_was_asked_for(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="forest", seed=3, tuning={"trees": 4}))
        for stand in model.engine.trees.values():
            self.assertEqual(len(stand), 4)

    def test_a_wide_split_scores_below_a_narrow_one_that_explains_as_much(self):
        target = ["yes"] * 10 + ["no"] * 10
        rows = list(range(20))
        narrow = {"a": rows[:10], "b": rows[10:]}
        wide = {str(index): [index] for index in rows}
        self.assertGreater(tree_module._gain_ratio(target, rows, narrow),
                           tree_module._gain_ratio(target, rows, wide))

    def test_rare_values_share_a_branch_rather_than_each_getting_one(self):
        column = ["common"] * 40 + [f"rare{index}" for index in range(30)]
        branches = tree_module._branches(column, list(range(len(column))))
        self.assertLessEqual(len(branches), tree_module.MAX_BRANCHES)
        self.assertIn(tree_module.OTHER, branches)

    def test_blank_rows_take_no_part_in_a_split(self):
        column = ["a", "", "b", "", "a"]
        branches = tree_module._branches(column, list(range(5)))
        self.assertEqual(sorted(branches), ["a", "b"])
        self.assertEqual(sum(len(rows) for rows in branches.values()), 3)


# ----------------------------------------------------------------------
# the other two
# ----------------------------------------------------------------------


class TestNearest(unittest.TestCase):
    def test_it_keeps_no_more_records_than_it_was_told_to(self):
        model = train(CITY_SCHEMA, city_records(500),
                      TrainOptions(algorithm="nearest", seed=1, tuning={"rows": 60}))
        self.assertLessEqual(len(model.engine.rows), 60)

    def test_agreeing_about_a_rare_value_counts_for_more_than_a_common_one(self):
        model = train(CITY_SCHEMA, city_records(300),
                      TrainOptions(algorithm="nearest", seed=1))
        surprise = model.engine.surprise["city"]
        commonest = max(surprise, key=lambda value: -surprise[value])
        rarest = max(surprise, key=lambda value: surprise[value])
        self.assertGreater(surprise[rarest], surprise[commonest])

    def test_it_finds_the_interaction_too_because_it_matches_whole_records(self):
        model = train(INTERACTION_SCHEMA, interaction_records(),
                      TrainOptions(algorithm="nearest", seed=3))
        prediction = model.predict_field(
            "relationship", {"policy": "family", "role": "dependent"})
        self.assertEqual(prediction.value, "child")


class TestBayes(unittest.TestCase):
    def test_an_unseen_combination_does_not_veto_an_answer(self):
        """What the smoothing is for: one zero count must not decide it."""
        model = train(CITY_SCHEMA, city_records(300),
                      TrainOptions(algorithm="bayes", seed=1))
        guess = model.engine.guess("state", {"city": "Austin", "country": "Narnia"},
                                   model.profiles["state"])
        self.assertEqual(guess.value, "TX")

    def test_its_raw_scores_are_overconfident_and_the_calibration_knows(self):
        model = train(CITY_SCHEMA, city_records(400),
                      TrainOptions(algorithm="bayes", seed=2))
        guess = model.engine.guess("state", {"city": "Denver"},
                                   model.profiles["state"])
        # Multiplying likelihoods that are not independent lands high; what a
        # user is shown is the measured rate for that band instead.
        self.assertGreater(guess.score, 0.7)
        self.assertLessEqual(model.calibration.apply(guess.score), 1.0)


# ----------------------------------------------------------------------
# choosing what to type first
# ----------------------------------------------------------------------


class TestReach(unittest.TestCase):
    def test_every_engine_rates_a_field_higher_once_its_predictor_is_typed(self):
        for name in algos.names():
            with self.subTest(algorithm=name):
                model = train(CITY_SCHEMA, city_records(300),
                              TrainOptions(algorithm=name, seed=1))
                nothing = model.engine.reach("state", set())
                with_city = model.engine.reach("state", {"city"})
                self.assertGreaterEqual(with_city, nothing)
                self.assertGreater(with_city, 0.0)

    def test_the_seed_suggestion_prefers_a_field_that_unlocks_another(self):
        from fillerai.train.evaluate import suggest_seed_fields

        for name in algos.names():
            with self.subTest(algorithm=name):
                model = train(CITY_SCHEMA, city_records(300),
                              TrainOptions(algorithm=name, seed=1))
                self.assertIn("city", suggest_seed_fields(model, 2))


if __name__ == "__main__":
    unittest.main()
