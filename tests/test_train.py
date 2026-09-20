"""Tests for the autofill model. Run with: python -m unittest discover -s tests"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.cli import _load_records, main
from fillerai.schema import Constraints, Field, FormSchema, Option
from fillerai.train import associate, derive, features
from fillerai.train.evaluate import evaluate, suggest_seed_fields
from fillerai.train.model import (
    ACCEPT_ABOVE,
    USUAL_FLOOR,
    AutofillModel,
    Calibration,
    TrainOptions,
    _isotonic,
    split_records,
    train,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def make_schema(*fields: Field) -> FormSchema:
    return FormSchema(name="t", fields=list(fields))


def text_field(name: str, **kwargs) -> Field:
    constraints = kwargs.pop("constraints", None) or Constraints()
    return Field(name=name, constraints=constraints, **kwargs)


def profiles_for(schema: FormSchema, records: list[dict]) -> dict:
    columns = {f.name: features.column(records, f.name) for f in schema.fields}
    return columns, {f.name: features.profile_field(f, columns[f.name])
                     for f in schema.fields}


class TestFeatures(unittest.TestCase):
    def test_booleans_normalise_to_words_so_json_and_csv_agree(self):
        self.assertEqual(features.normalise(True), "true")
        self.assertEqual(features.normalise(False), "false")
        # A CSV round trip turns the same record into these strings, and the
        # two must count as the same value.
        self.assertEqual(features.normalise("true"), features.normalise(True))

    def test_multi_values_join_the_way_the_csv_writer_joins_them(self):
        self.assertEqual(features.normalise(["a", "b"]), "a|b")

    def test_missing_and_none_are_both_blank(self):
        self.assertEqual(features.normalise(None), "")
        self.assertEqual(features.column([{}], "absent"), [""])

    def test_a_select_is_enumerable_even_with_one_row(self):
        field = text_field("s", control="select", options=[Option("a"), Option("b")])
        # The form states its own closed set; that beats anything counted off
        # a sample this thin.
        self.assertEqual(features.profile_field(field, ["a"]).kind, "enumerable")

    def test_a_field_different_in_every_record_is_open(self):
        profile = features.profile_field(
            text_field("claim"), [f"CLM-{i:06d}" for i in range(200)])
        self.assertEqual(profile.kind, "open")
        self.assertEqual(profile.format, "AAA-999999")

    def test_a_field_with_one_value_is_constant(self):
        profile = features.profile_field(text_field("c"), ["US"] * 50)
        self.assertEqual(profile.kind, "constant")
        self.assertEqual(profile.modal(), ("US", 1.0))

    def test_blanks_are_excluded_from_the_distribution(self):
        profile = features.profile_field(text_field("c"), ["a", "a", "", ""])
        self.assertEqual(profile.fill_rate, 0.5)
        self.assertEqual(profile.distribution(), {"a": 1.0})

    def test_a_mask_is_only_kept_when_the_values_agree_on_one(self):
        self.assertIsNone(features.dominant_mask(["AB-12", "1234567", "x"]))
        self.assertEqual(features.dominant_mask(["12-34"] * 10), "99-99")

    def test_a_profile_survives_the_round_trip_through_json(self):
        profile = features.profile_field(text_field("c"), ["a", "b", "a"])
        again = features.Profile.from_dict(json.loads(json.dumps(profile.to_dict())))
        self.assertEqual(again.distribution(), profile.distribution())
        self.assertEqual(again.kind, profile.kind)


class TestAssociate(unittest.TestCase):
    def learn(self, columns):
        profiles = {
            name: features.profile_field(text_field(name), values)
            for name, values in columns.items()
        }
        return associate.learn_links(columns, profiles)

    def test_a_perfect_predictor_scores_one(self):
        places = [("Austin", "TX"), ("Dallas", "TX"), ("Miami", "FL"),
                  ("Denver", "CO"), ("Boise", "ID")]
        rows = [places[i % len(places)] for i in range(100)]
        links = self.learn({"city": [c for c, _ in rows], "state": [s for _, s in rows]})
        self.assertEqual(links["state"][0].source, "city")
        self.assertEqual(round(links["state"][0].strength, 3), 1.0)

    def test_a_constant_target_teaches_nothing(self):
        # Every row says "US", so guessing it is already perfect and no
        # predictor can improve on that.
        links = self.learn({"a": [f"v{i % 5}" for i in range(60)],
                            "country": ["US"] * 60})
        self.assertEqual(links.get("country", []), [])

    def test_a_unique_key_does_not_score_for_memorising(self):
        """The regression that matters most.

        Scored on the rows its own counts came from, a field with a unique
        value per record predicts every one of them perfectly - and predicts
        nothing at all on a row it has not seen. Before this was corrected,
        a ZIP code came out as a strong predictor of an unrelated field on
        the other side of the form.
        """
        rows = 200
        links = self.learn({
            "unique_id": [f"ID{i:05d}" for i in range(rows)],
            "coin": ["heads" if i % 2 else "tails" for i in range(rows)],
        })
        self.assertEqual(links.get("coin", []), [])

    def test_two_independent_fields_do_not_become_predictors(self):
        # Buckets of two or three agree by luck often enough to look like a
        # relationship; the shuffled baseline is what prices that in.
        rng = random.Random(11)
        links = self.learn({
            "letter": [rng.choice("abcdefghij") for _ in range(400)],
            "choice": [rng.choice(("one", "two", "three")) for _ in range(400)],
        })
        self.assertEqual(links.get("choice", []), [])
        self.assertEqual(links.get("letter", []), [])

    def test_pairs_ignore_rows_where_either_side_is_blank(self):
        joint, paired = associate._pair_counts(["x", "", "x"], ["1", "2", ""])
        self.assertEqual(paired, 1)
        self.assertEqual(dict(joint["x"]), {"1": 1})

    def test_leave_one_out_scores_a_singleton_bucket_at_nothing(self):
        self.assertEqual(associate._loo_hits(Counter({"a": 1})), 0)
        self.assertEqual(associate._loo_hits(Counter({"a": 5})), 5)
        # Three against one: only the majority rows survive their own removal.
        self.assertEqual(associate._loo_hits(Counter({"a": 3, "b": 1})), 3)
        # A tie is not a prediction.
        self.assertEqual(associate._loo_hits(Counter({"a": 2, "b": 2})), 0)

    def test_a_kept_bucket_reports_the_mass_it_dropped(self):
        link = associate.Link(source="a", target="b", strength=1.0, support=10,
                              table={"x": (10, [("p", 6), ("q", 1)])})
        distribution, rows = link.conditional("x")
        self.assertEqual(rows, 10)
        # 6 of the bucket's 10 rows, not 6 of the 7 that were kept: the tail
        # is missing mass, and a prediction over it should be less sure.
        self.assertAlmostEqual(distribution["p"], 0.6)

    def test_an_unseen_predictor_value_yields_nothing(self):
        link = associate.Link(source="a", target="b", strength=1.0, support=10,
                              table={"x": (10, [("p", 10)])})
        self.assertEqual(link.conditional("never-seen"), ({}, 0))

    def test_a_link_survives_the_round_trip_through_json(self):
        link = associate.Link(source="a", target="b", strength=0.5, support=9,
                              table={"x": (3, [("p", 2), ("q", 1)])})
        again = associate.Link.from_dict(json.loads(json.dumps(link.to_dict())))
        self.assertEqual(again.conditional("x"), link.conditional("x"))


class TestDerivations(unittest.TestCase):
    def build(self, records, *fields):
        schema = make_schema(*fields)
        columns, profiles = profiles_for(schema, records)
        return derive.learn_derivations(schema, records, profiles, columns)

    def test_a_full_name_is_recognised_as_its_parts_joined(self):
        records = [{"first": "Ada", "last": "Lovelace", "full": "Ada Lovelace"},
                   {"first": "Alan", "last": "Turing", "full": "Alan Turing"}] * 6
        rules = self.build(
            records,
            text_field("first", semantic_type="first_name"),
            text_field("last", semantic_type="last_name"),
            text_field("full", semantic_type="full_name"),
        )
        self.assertEqual(rules["full"].kind, "join")
        # The point of a rule over a count: it is right on a name it has
        # never seen.
        self.assertEqual(rules["full"].apply({"first": "Grace", "last": "Hopper"}),
                         "Grace Hopper")

    def test_a_rule_the_data_disagrees_with_is_dropped(self):
        # The field is labelled a full name but holds something else, so the
        # proposal fails its check and nothing survives.
        records = [{"first": "Ada", "last": "Lovelace", "full": f"HANDLE{i}"}
                   for i in range(20)]
        rules = self.build(
            records,
            text_field("first", semantic_type="first_name"),
            text_field("last", semantic_type="last_name"),
            text_field("full", semantic_type="full_name"),
        )
        self.assertNotIn("full", rules)

    def test_age_comes_from_the_date_of_birth(self):
        today = dt.date.today()
        records = []
        for offset in range(20, 45):
            born = dt.date(today.year - offset, 1, 1)
            age = (today.year - born.year
                   - ((today.month, today.day) < (born.month, born.day)))
            records.append({"dob": born.isoformat(), "age": str(age)})
        rules = self.build(
            records,
            text_field("dob", semantic_type="date_of_birth"),
            text_field("age", semantic_type="age"),
        )
        self.assertEqual(rules["age"].kind, "age")
        self.assertEqual(rules["age"].accuracy, 1.0)

    def test_a_copied_field_is_found_wherever_it_sits(self):
        records = [{"email": f"a{i}@example.com", "confirm": f"a{i}@example.com"}
                   for i in range(20)]
        rules = self.build(records, text_field("email"), text_field("confirm"))
        self.assertEqual(rules["confirm"].kind, "copy")
        self.assertEqual(rules["confirm"].inputs, ["email"])

    def test_two_fields_copying_each_other_do_not_deadlock(self):
        records = [{"a": str(i), "b": str(i)} for i in range(20)]
        rules = self.build(records, text_field("a"), text_field("b"))
        # Both directions verify. Keeping both would leave prediction with no
        # field it could start from.
        self.assertEqual(len(rules), 1)

    def test_an_initial_is_only_proposed_for_a_one_character_box(self):
        names = ("Sarah", "Michael", "Jo", "Ann", "Bo", "Cleo", "Dev")
        records = [{"middle_name": n, "mi": n[0], "note": f"{n}!"} for n in names] * 3
        rules = self.build(
            records,
            text_field("middle_name", semantic_type="middle_name"),
            text_field("mi", semantic_type="middle_name",
                       constraints=Constraints(max_length=1)),
            text_field("note", semantic_type="free_text"),
        )
        self.assertEqual(rules["mi"].kind, "initial")
        self.assertEqual(rules["mi"].apply({"middle_name": "Quinn"}), "Q")
        # "note" starts with the same letter every time, but the form never
        # said it was one character wide, so no initial is proposed for it.
        self.assertNotIn("note", rules)

    def test_a_rule_never_crosses_from_one_person_to_another(self):
        # The spouse's surname must not be joined onto the applicant's first
        # name. The groups phase 1 found are what keep those apart.
        records = [{"first": "Ada", "last": "Lovelace",
                    "spouse_full": "Chris Byron"} for _ in range(20)]
        rules = self.build(
            records,
            Field(name="first", semantic_type="first_name", group="applicant"),
            Field(name="last", semantic_type="last_name", group="applicant"),
            Field(name="spouse_full", semantic_type="full_name", group="spouse"),
        )
        self.assertNotIn("spouse_full", rules)

    def test_an_email_rule_needs_one_domain_and_one_style(self):
        records = [{"first": f"user{i}", "last": "smith",
                    "email": f"user{i}.smith@acme.test"} for i in range(20)]
        rules = self.build(
            records,
            text_field("first", semantic_type="first_name"),
            text_field("last", semantic_type="last_name"),
            text_field("email", semantic_type="email"),
        )
        self.assertEqual(rules["email"].kind, "email")
        self.assertEqual(rules["email"].apply({"first": "Dana", "last": "Klein"}),
                         "dana.klein@acme.test")

    def test_addresses_spread_over_many_domains_yield_no_email_rule(self):
        hosts = ("a.test", "b.test", "c.test", "d.test")
        records = [{"first": f"u{i}", "last": "smith",
                    "email": f"u{i}.smith@{hosts[i % 4]}"} for i in range(40)]
        rules = self.build(
            records,
            text_field("first", semantic_type="first_name"),
            text_field("last", semantic_type="last_name"),
            text_field("email", semantic_type="email"),
        )
        self.assertNotIn("email", rules)

    def test_a_rule_declines_when_an_input_it_needs_is_missing(self):
        rule = derive.Derivation(kind="join", target="full",
                                 inputs=["first", "last"],
                                 params={"template": "{0} {1}"})
        self.assertIsNone(rule.apply({"first": "Ada"}))
        self.assertEqual(rule.apply({"first": "Ada", "last": "Byron"}), "Ada Byron")

    def test_dates_are_read_in_whichever_shape_the_form_used(self):
        self.assertEqual(derive.parse_date("2020-03-04"), dt.date(2020, 3, 4))
        self.assertEqual(derive.parse_date("03/04/2020"), dt.date(2020, 3, 4))
        self.assertIsNone(derive.parse_date("not a date"))

    def test_a_derivation_survives_the_round_trip_through_json(self):
        rule = derive.Derivation(kind="initial", target="mi", inputs=["middle"],
                                 params={"upper": True, "suffix": "."},
                                 accuracy=1.0, support=30)
        again = derive.Derivation.from_dict(json.loads(json.dumps(rule.to_dict())))
        self.assertEqual(again.apply({"middle": "quinn"}), "Q.")


class TestModel(unittest.TestCase):
    def setUp(self):
        self.schema = make_schema(
            Field(name="city", semantic_type="city"),
            Field(name="state", semantic_type="state"),
            Field(name="country", semantic_type="country"),
            Field(name="ref", semantic_type="free_text"),
        )
        places = [("Austin", "TX"), ("Dallas", "TX"), ("Miami", "FL"),
                  ("Denver", "CO"), ("Boise", "ID")]
        self.records = [
            {"city": places[i % 5][0], "state": places[i % 5][1],
             "country": "US", "ref": f"R{i:05d}"}
            for i in range(120)
        ]
        self.model = train(self.schema, self.records, TrainOptions(seed=1))

    def test_a_known_city_gives_its_state_with_confidence(self):
        prediction = self.model.predict_field("state", {"city": "Miami"})
        self.assertEqual(prediction.value, "FL")
        self.assertEqual(prediction.basis, "learned")
        self.assertGreaterEqual(prediction.confidence, ACCEPT_ABOVE)
        self.assertIn("Miami", prediction.because[0])

    def test_a_field_that_never_repeats_is_declined_with_its_shape(self):
        prediction = self.model.predict_field("ref", {"city": "Miami"})
        self.assertFalse(prediction.known)
        self.assertEqual(prediction.format, "A99999")
        self.assertIn("every record", prediction.because[0])

    def test_a_constant_field_answers_with_nothing_given(self):
        prediction = self.model.predict_field("country", {})
        self.assertEqual(prediction.value, "US")
        self.assertEqual(prediction.basis, "usual")

    def test_predict_leaves_out_what_the_caller_already_typed(self):
        predictions = self.model.predict({"city": "Austin"})
        self.assertNotIn("city", predictions)
        self.assertIn("state", predictions)

    def test_a_blank_value_is_not_treated_as_something_the_caller_typed(self):
        predictions = self.model.predict({"city": ""})
        self.assertIn("city", predictions)

    def test_filled_returns_the_record_the_model_would_hand_back(self):
        record = self.model.filled({"city": "Denver"})
        self.assertEqual(record["state"], "CO")
        self.assertEqual(record["country"], "US")
        self.assertNotIn("ref", record)  # it declined, so it added nothing

    def test_a_flat_marginal_is_not_dressed_up_as_a_prediction(self):
        # Every value is rare, so "the commonest" is not an answer.
        schema = make_schema(Field(name="pick"))
        model = train(schema, [{"pick": f"v{i % 20}"} for i in range(200)],
                      TrainOptions(seed=1))
        prediction = model.predict_field("pick", {})
        self.assertFalse(prediction.known)
        self.assertIn("no usual value", prediction.because[0])
        self.assertLess(1 / 20, USUAL_FLOOR)

    def test_the_reason_names_the_value_that_was_chosen(self):
        # A reason read off a separate lookup drifts from the answer
        # wherever two values tie, and a reason that contradicts the value
        # beside it is worse than no reason at all.
        for name in self.model.targets():
            prediction = self.model.predict_field(name, {})
            if prediction.known and prediction.basis == "usual":
                self.assertIn(str(prediction.value), prediction.because[0])

    def test_alternatives_are_ranked_below_the_answer(self):
        prediction = self.model.predict_field("state", {})
        scores = [prediction.score] + [p for _, p in prediction.alternatives]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_model_survives_the_round_trip_through_json(self):
        reloaded = AutofillModel.from_json(self.model.to_json())
        for name in self.model.targets():
            self.assertEqual(
                self.model.predict_field(name, {"city": "Boise"}).to_dict(),
                reloaded.predict_field(name, {"city": "Boise"}).to_dict(),
            )

    def test_a_model_from_a_future_format_is_refused_rather_than_misread(self):
        data = self.model.to_dict()
        data["model_version"] = "99.0"
        with self.assertRaises(ValueError):
            AutofillModel.from_dict(data)

    def test_fitting_twice_on_the_same_data_gives_the_same_model(self):
        # A model you cannot diff is a model you cannot review.
        again = train(self.schema, self.records, TrainOptions(seed=1))
        self.assertEqual(again.to_json(), self.model.to_json())

    def test_read_only_fields_are_not_learned_or_predicted(self):
        schema = make_schema(
            Field(name="a"),
            Field(name="computed", constraints=Constraints(read_only=True)),
        )
        model = train(schema, [{"a": "x", "computed": "y"}] * 30)
        self.assertNotIn("computed", model.targets())
        self.assertNotIn("computed", model.predict({}))

    def test_training_needs_at_least_one_record(self):
        with self.assertRaises(ValueError):
            train(self.schema, [])

    def test_too_few_records_to_measure_on_means_no_calibration_claimed(self):
        model = train(self.schema, self.records[:6])
        self.assertFalse(model.calibration.fitted)
        self.assertEqual(model.held_out, 0)
        # An uncalibrated model still answers; it just does not pretend its
        # confidence has been checked.
        self.assertTrue(model.predict_field("state", {"city": "Austin"}).known)

    def test_the_split_keeps_every_record_exactly_once(self):
        fit, held = split_records(self.records, TrainOptions(holdout=0.25, seed=3))
        self.assertTrue(held)
        key = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in rows)
        self.assertEqual(key(fit + held), key(self.records))

    def test_the_field_report_covers_every_writable_field(self):
        report = self.model.field_report()
        self.assertEqual([r["name"] for r in report], self.model.targets())
        by_name = {r["name"]: r for r in report}
        self.assertEqual(by_name["ref"]["how"], "you")
        self.assertEqual(by_name["state"]["how"], "learned")
        self.assertEqual(by_name["country"]["how"], "usual")


class TestCalibration(unittest.TestCase):
    def test_the_curve_never_goes_down(self):
        out = _isotonic([0.1, 0.9, 0.2, 0.4], [10, 10, 10, 10])
        self.assertEqual(out, sorted(out))
        self.assertEqual(len(out), 4)

    def test_a_dip_is_pooled_with_its_neighbour_not_dropped(self):
        out = _isotonic([0.2, 0.8, 0.4], [1, 1, 1])
        self.assertAlmostEqual(out[1], out[2])
        self.assertAlmostEqual(out[1], 0.6)

    def test_an_already_rising_curve_is_left_alone(self):
        self.assertEqual(_isotonic([0.1, 0.2, 0.3], [1, 1, 1]), [0.1, 0.2, 0.3])

    def test_an_uncalibrated_model_passes_its_raw_score_through(self):
        self.assertEqual(Calibration().apply(0.42), 0.42)

    def test_confidence_means_what_it_says_on_records_never_seen(self):
        """The promise the model makes about itself, checked.

        Confidence is fitted on rows held back from the fit, so a band that
        claims 0.9 and upwards has to be right about that often on rows from
        a different generation run too.
        """
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        records = fillerai.generate(schema, count=400, seed=42).records
        model = train(schema, records, TrainOptions(seed=1))
        self.assertTrue(model.calibration.fitted)

        unseen = fillerai.generate(schema, count=150, seed=99).records
        report = evaluate(model, unseen, seeds=3)
        for band in report.reliability:
            if band["from"] >= 0.9 and band["count"] >= 30:
                self.assertGreater(band["accuracy"], 0.85)


class TestEvaluate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        records = fillerai.generate(cls.schema, count=300, seed=7).records
        cls.model = train(cls.schema, records, TrainOptions(seed=1))
        cls.unseen = fillerai.generate(cls.schema, count=80, seed=808).records

    def test_the_suggested_seeds_are_fields_worth_asking_for(self):
        seeds = suggest_seed_fields(self.model, 3)
        self.assertEqual(len(seeds), 3)
        self.assertEqual(len(set(seeds)), 3)
        for name in seeds:
            self.assertIn(name, self.model.profiles)
            # A field a rule already produces is free; asking the agent for
            # it spends the one thing being saved.
            self.assertNotIn(name, self.model.derivations)

    def test_asking_for_more_never_covers_less(self):
        one = evaluate(self.model, self.unseen, seeds=1)
        three = evaluate(self.model, self.unseen, seeds=3)
        self.assertGreaterEqual(three.accepted, one.accepted)

    def test_what_it_fills_confidently_it_gets_right(self):
        report = evaluate(self.model, self.unseen, seeds=3)
        self.assertGreater(report.accepted, 0)
        self.assertGreater(report.accepted_accuracy, 0.9)

    def test_declining_is_counted_separately_from_being_wrong(self):
        report = evaluate(self.model, self.unseen, seeds=3)
        self.assertGreater(report.declined, 0)
        self.assertEqual(report.attempted + report.declined, report.checkable)
        self.assertIn("no answer to give", report.headline())

    def test_a_blank_in_the_source_is_not_scored_as_a_miss(self):
        # A blank says the field did not apply to that record, not that the
        # model was wrong about it.
        schema = make_schema(Field(name="a"), Field(name="b"))
        model = train(schema, [{"a": "x", "b": "y"}] * 30)
        report = evaluate(model, [{"a": "x", "b": ""}], seeds=["a"])
        self.assertEqual(report.checkable, 0)

    def test_seed_fields_the_model_does_not_have_are_ignored(self):
        self.assertEqual(
            evaluate(self.model, self.unseen, seeds=["nonexistent"]).seeds, [])

    def test_the_report_is_json_ready(self):
        json.dumps(evaluate(self.model, self.unseen, seeds=2).to_dict())

    def test_seed_suggestions_are_the_same_every_time(self):
        self.assertEqual(suggest_seed_fields(self.model, 3),
                         suggest_seed_fields(self.model, 3))


class TestBundledExample(unittest.TestCase):
    """The enrollment example exists to exercise the rules, so it must."""

    @classmethod
    def setUpClass(cls):
        cls.schema = fillerai.extract_spec(EXAMPLES / "member_enrollment.fields.json")
        records = fillerai.generate(cls.schema, count=500, seed=42).records
        cls.model = train(cls.schema, records, TrainOptions(seed=1))
        cls.unseen = fillerai.generate(cls.schema, count=120, seed=777).records

    def test_the_generated_records_still_satisfy_the_form(self):
        records = fillerai.generate(self.schema, count=100, seed=5).records
        self.assertEqual(fillerai.validate(self.schema, records), [])
        self.assertEqual(fillerai.coherence_report(self.schema, records), [])

    def test_every_kind_of_rule_is_found_and_is_right(self):
        rules = self.model.derivations
        self.assertEqual(rules["member_age"].kind, "age")
        self.assertEqual(rules["member_full_name"].kind, "join")
        self.assertEqual(rules["member_middle_initial"].kind, "initial")
        for name in ("member_age", "member_middle_initial"):
            self.assertEqual(rules[name].accuracy, 1.0)

    def test_the_rules_answer_a_person_never_seen_in_training(self):
        typed = {
            "member_first_name": "Dana",
            "member_middle_name": "Rae",
            "member_last_name": "Okonkwo",
            "member_date_of_birth": "1987-04-12",
        }
        predictions = self.model.predict(typed)
        self.assertEqual(predictions["member_full_name"].value, "Dana Rae Okonkwo")
        self.assertEqual(predictions["member_middle_initial"].value, "R")
        self.assertEqual(predictions["member_full_name"].basis, "rule")

        today = dt.date.today()
        born = dt.date(1987, 4, 12)
        expected = (today.year - born.year
                    - ((today.month, today.day) < (born.month, born.day)))
        self.assertEqual(predictions["member_age"].value, str(expected))

    def test_the_greedy_search_assembles_a_rule_that_needs_three_fields(self):
        """Scored strictly, the first name of a three-part rule buys nothing.

        A full name derives from a first, middle and last name together, so
        no single one of them raises what the model can answer, and a search
        that only credits finished rules never reaches the second or third.
        """
        parts = {"member_first_name", "member_middle_name", "member_last_name"}
        # Which seed completes the set depends on what else the form offers,
        # so the claim is that the search gets there, not that it gets there
        # on any particular round.
        self.assertFalse(parts <= set(suggest_seed_fields(self.model, 2)))
        self.assertLessEqual(parts, set(suggest_seed_fields(self.model, 6)))

        filled = self.model.filled({"member_first_name": "Dana",
                                    "member_middle_name": "Rae",
                                    "member_last_name": "Okonkwo"})
        self.assertEqual(filled["member_full_name"], "Dana Rae Okonkwo")

    def test_the_address_relationships_are_learned_too(self):
        self.assertIn("home_postal_code",
                      [l.source for l in self.model.links["home_city"]])
        report = evaluate(self.model, self.unseen, seeds=3)
        self.assertGreater(report.accepted_accuracy, 0.95)


class TestLibraryAndCli(unittest.TestCase):
    def test_the_documented_library_call_works(self):
        schema = fillerai.extract_spec(EXAMPLES / "patient_registration.fields.json")
        records = fillerai.generate(schema, count=120, seed=3).records
        model = fillerai.train(schema, records, seed=1)
        seeds = fillerai.suggest_seed_fields(model, 2)
        self.assertTrue(seeds)
        self.assertIsInstance(
            fillerai.evaluate(model, records, seeds=seeds).to_dict(), dict)

    def test_train_predict_and_evaluate_round_trip_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            schema_path, data_path = str(out / "schema.json"), str(out / "data.csv")
            unseen_path, model_path = str(out / "unseen.json"), str(out / "model.json")
            record_path = str(out / "record.json")

            self.assertEqual(main(["extract", str(EXAMPLES / "claims_intake.html"),
                                   "-o", schema_path]), 0)
            self.assertEqual(main(["generate", schema_path, "-n", "300", "--seed", "4",
                                   "-f", "csv", "-o", data_path]), 0)
            self.assertEqual(main(["generate", schema_path, "-n", "40", "--seed", "77",
                                   "-o", unseen_path]), 0)
            self.assertEqual(main(["train", schema_path, data_path, "--seed", "1",
                                   "-o", model_path, "--evaluate", unseen_path]), 0)
            self.assertEqual(main(["evaluate", model_path, unseen_path]), 0)

            model = AutofillModel.from_json(Path(model_path).read_text())
            seed = suggest_seed_fields(model, 1)[0]
            value = next(r[seed] for r in json.loads(
                Path(unseen_path).read_text())["records"] if r[seed])
            self.assertEqual(main(["predict", model_path, "--set", f"{seed}={value}",
                                   "-f", "record", "-o", record_path]), 0)
            self.assertEqual(json.loads(Path(record_path).read_text())[seed], value)

    def test_a_dataset_read_from_csv_trains_like_the_json_it_came_from(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        dataset = fillerai.generate(schema, count=200, seed=11)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "data.csv"
            csv_path.write_text(dataset.to_csv(), encoding="utf-8")
            from_csv = _load_records(csv_path)

        from_json = train(schema, dataset.records, TrainOptions(seed=1))
        from_file = train(schema, from_csv, TrainOptions(seed=1))
        sources = lambda m: {n: [l.source for l in m.links.get(n, [])]
                             for n in m.targets()}
        self.assertEqual(sources(from_json), sources(from_file))

    def test_ndjson_and_a_bare_list_are_both_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ndjson = Path(tmp) / "d.ndjson"
            ndjson.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
            self.assertEqual(_load_records(ndjson), [{"a": 1}, {"a": 2}])
            plain = Path(tmp) / "d.json"
            plain.write_text('[{"a": 1}]', encoding="utf-8")
            self.assertEqual(_load_records(plain), [{"a": 1}])

    def test_predict_rejects_a_field_the_model_has_never_heard_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "m.json"
            schema = make_schema(Field(name="a"), Field(name="b"))
            model = train(schema, [{"a": "x", "b": "y"}] * 30)
            model_path.write_text(model.to_json(), encoding="utf-8")
            self.assertEqual(main(["predict", str(model_path), "--set", "nope=1"]), 1)
            # ...and a --set without an "=" is a mistake worth naming too.
            self.assertEqual(main(["predict", str(model_path), "--set", "novalue"]), 1)


class TestCompatibility(unittest.TestCase):
    """A model saved before the engine was selectable still has to load.

    There are model files on disk from phase 3, and the numbers in them did
    not change when the engine moved into its own key - only where they are
    written down. Refusing to read them would be a migration for no reason.
    """

    def model(self):
        schema = make_schema(
            text_field("city", semantic_type="city"),
            text_field("state", semantic_type="state"),
        )
        pairs = [("Austin", "TX"), ("Denver", "CO")]
        records = [{"city": pairs[i % 2][0], "state": pairs[i % 2][1]}
                   for i in range(80)]
        return schema, records, train(schema, records, TrainOptions(seed=1))

    def test_a_one_point_oh_file_loads_into_the_conditional_tables(self):
        _schema, _records, model = self.model()
        # Write it out the way phase 3 did: links at the top level, no
        # algorithm named, because there was only one.
        old = model.to_dict()
        old["model_version"] = "1.0"
        old["links"] = [l.to_dict() for group in model.engine.links.values()
                        for l in group]
        del old["engine"]
        del old["algorithm"]

        loaded = AutofillModel.from_dict(old)
        self.assertEqual(loaded.algorithm, "statistical")
        self.assertEqual(loaded.predict_field("state", {"city": "Austin"}).value, "TX")
        self.assertEqual(sorted(loaded.links), sorted(model.engine.links))

    def test_a_reloaded_model_is_written_out_at_the_current_version(self):
        _schema, _records, model = self.model()
        self.assertEqual(AutofillModel.from_json(model.to_json()).model_version,
                         model.to_dict()["model_version"])

    def test_a_model_from_a_future_format_is_still_refused(self):
        _schema, _records, model = self.model()
        data = model.to_dict()
        data["model_version"] = "9.0"
        with self.assertRaises(ValueError):
            AutofillModel.from_dict(data)

    def test_links_are_still_reachable_on_a_statistical_model(self):
        _schema, _records, model = self.model()
        self.assertIn("state", model.links)

    def test_an_engine_without_links_reports_none_rather_than_failing(self):
        schema = make_schema(
            text_field("city", semantic_type="city"),
            text_field("state", semantic_type="state"),
        )
        pairs = [("Austin", "TX"), ("Denver", "CO")]
        records = [{"city": pairs[i % 2][0], "state": pairs[i % 2][1]}
                   for i in range(80)]
        model = train(schema, records, TrainOptions(algorithm="tree", seed=1))
        self.assertEqual(model.links, {})


class TestRunRecord(unittest.TestCase):
    """What a run says about itself afterwards."""

    def model(self, **options):
        schema = make_schema(
            text_field("city", semantic_type="city"),
            text_field("state", semantic_type="state"),
        )
        pairs = [("Austin", "TX"), ("Denver", "CO")]
        records = [{"city": pairs[i % 2][0], "state": pairs[i % 2][1]}
                   for i in range(80)]
        return train(schema, records, TrainOptions(seed=1, **options))

    def test_a_model_records_what_it_was_asked_for(self):
        model = self.model(algorithm="forest", tuning={"trees": 3})
        self.assertEqual(model.settings["algorithm"], "forest")
        self.assertEqual(model.settings["tuning"], {"trees": 3})
        self.assertEqual(model.settings["seed"], 1)

    def test_the_settings_survive_the_round_trip(self):
        model = self.model(algorithm="forest", tuning={"trees": 3})
        self.assertEqual(AutofillModel.from_json(model.to_json()).settings,
                         model.settings)

    def test_the_field_report_says_how_this_engine_would_put_it(self):
        model = self.model(algorithm="nearest")
        row = next(r for r in model.field_report() if r["name"] == "state")
        self.assertEqual(row["how"], "learned")
        self.assertIn("matched on", row["detail"])

    def test_a_field_nothing_answers_carries_no_engine_wording(self):
        model = self.model()
        rows = [r for r in model.field_report() if r["how"] == "you"]
        for row in rows:
            self.assertEqual(row["detail"], "")


class TestScript(unittest.TestCase):
    """The script a run hands back has to actually be a script."""

    def test_it_compiles_for_every_algorithm(self):
        from fillerai.train import algos, script as script_writer

        for name in algos.names():
            with self.subTest(algorithm=name):
                options = TrainOptions(algorithm=name, seed=9)
                text = script_writer.script(options)
                compile(text, "generated.py", "exec")
                self.assertIn(repr(name), text)
                self.assertIn("seed=9", text)

    def test_the_settings_asked_for_appear_in_it(self):
        from fillerai.train import script as script_writer

        options = TrainOptions(algorithm="forest", tuning={"trees": 16},
                               use_rules=False)
        text = script_writer.script(options)
        compile(text, "generated.py", "exec")
        self.assertIn("'trees': 16", text)
        self.assertIn("use_rules=False", text)

    def test_the_command_is_the_same_run_as_one_line(self):
        from fillerai.train import script as script_writer

        options = TrainOptions(algorithm="tree", seed=4, tuning={"max_depth": 6})
        command = script_writer.command(options)
        self.assertIn("--algorithm tree", command)
        self.assertIn("--seed 4", command)
        self.assertIn("--set max_depth=6", command)

    def test_a_script_with_no_seed_says_how_to_pin_one(self):
        from fillerai.train import script as script_writer

        text = script_writer.script(TrainOptions())
        compile(text, "generated.py", "exec")
        self.assertIn("pin this", text)


if __name__ == "__main__":
    unittest.main()
