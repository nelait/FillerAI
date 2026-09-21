"""Tests for the rules a form declares about itself.

A generated dropdown used to be a coin toss. That is why synthetic data made
from a bare field list taught a model addresses and almost nothing else: the
relationships it could find were the only ones the generator had put there,
and the generator put there only the ones that follow from being a person.

These cover the other half - a form saying what its own fields follow - in
three parts. That the rule is honoured in the data, that a rule the spec got
wrong is reported rather than quietly turned back into noise, and that the
two bundled forms built on rules are still worth the claim made for them.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.extract import spec as spec_loader
from fillerai.generate.dataset import (
    Options, coherence_report, generate, validate,
)
from fillerai.schema import Derived, Field, FormSchema
from fillerai.train.evaluate import evaluate, suggest_seed_fields
from fillerai.train.model import TrainOptions, split_records, train

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def spec(*fields: dict) -> FormSchema:
    return spec_loader.load({"name": "rules test", "fields": list(fields)})


class TestReadingARule(unittest.TestCase):
    def test_follows_and_when_become_a_rule(self):
        schema = spec(
            {"name": "tier", "control": "select", "options": ["Gold", "Bronze"]},
            {"name": "deductible", "control": "select", "options": ["500", "2500"],
             "follows": "tier", "when": {"Gold": "500", "Bronze": "2500"}},
        )
        rule = schema.field("deductible").derived
        self.assertEqual(rule.sources, ("tier",))
        self.assertEqual(rule.lookup(["Gold"]), ("500",))

    def test_a_value_may_be_a_list_which_narrows_rather_than_decides(self):
        schema = spec({"name": "title", "follows": "team",
                       "when": {"Eng": ["Engineer", "SRE"]}})
        self.assertEqual(schema.field("title").derived.lookup(["Eng"]),
                         ("Engineer", "SRE"))

    def test_the_value_written_and_the_value_looked_up_need_not_match_case(self):
        schema = spec({"name": "x", "follows": "y", "when": {"Gold": "1"}})
        self.assertEqual(schema.field("x").derived.lookup(["  gOLd "]), ("1",))

    def test_several_sources_key_on_their_values_together(self):
        schema = spec({"name": "band", "follows": ["tier", "klass"],
                       "when": {"Gold|Truck": "D"}, "otherwise": "A"})
        rule = schema.field("band").derived
        self.assertEqual(rule.sources, ("tier", "klass"))
        self.assertEqual(rule.lookup(["Gold", "Truck"]), ("D",))
        self.assertEqual(rule.lookup(["Gold", "Sedan"]), ("A",))

    def test_a_rule_survives_the_round_trip_through_a_saved_schema(self):
        schema = spec({"name": "x", "follows": "y", "when": {"a": ["1", "2"]}})
        again = FormSchema.from_dict(schema.to_dict())
        self.assertEqual(again.field("x").derived, schema.field("x").derived)

    def test_a_field_with_no_rule_has_none(self):
        self.assertIsNone(spec({"name": "plain"}).field("plain").derived)


class TestApplyingARule(unittest.TestCase):
    def setUp(self):
        self.schema = spec(
            {"name": "tier", "control": "select", "required": True,
             "options": ["Gold", "Bronze"]},
            {"name": "deductible", "control": "select", "options": ["500", "2500"],
             "follows": "tier", "when": {"Gold": "500", "Bronze": "2500"}},
        )

    def test_the_rule_holds_in_every_record(self):
        records = generate(self.schema, Options(count=80, seed=3, blank_rate=0)).records
        self.assertEqual(
            {(r["tier"], r["deductible"]) for r in records},
            {("Gold", "500"), ("Bronze", "2500")},
        )
        self.assertEqual(coherence_report(self.schema, records), [])

    def test_a_source_below_its_dependent_is_still_rendered_first(self):
        # The form asks for the deductible above the tier that decides it,
        # which is a thing real forms do.
        schema = spec(
            {"name": "deductible", "control": "select", "options": ["500", "2500"],
             "follows": "tier", "when": {"Gold": "500", "Bronze": "2500"}},
            {"name": "tier", "control": "select", "required": True,
             "options": ["Gold", "Bronze"]},
        )
        records = generate(schema, Options(count=40, seed=5, blank_rate=0)).records
        self.assertEqual(coherence_report(schema, records), [])

    def test_a_record_keeps_the_order_the_form_asks_in(self):
        schema = spec(
            {"name": "deductible", "follows": "tier", "when": {"Gold": "500"}},
            {"name": "tier", "control": "select", "options": ["Gold"], "required": True},
        )
        record = generate(schema, Options(count=1, seed=1, blank_rate=0)).records[0]
        self.assertEqual(list(record), ["deductible", "tier"])

    def test_a_chain_of_rules_resolves_end_to_end(self):
        schema = spec(
            {"name": "make", "control": "select", "required": True, "options": ["Tesla"]},
            {"name": "model", "control": "select", "options": ["Model Y"],
             "follows": "make", "when": {"Tesla": "Model Y"}},
            {"name": "body", "control": "select", "options": ["SUV"],
             "follows": "model", "when": {"Model Y": "SUV"}},
        )
        record = generate(schema, Options(count=1, seed=1, blank_rate=0)).records[0]
        self.assertEqual((record["make"], record["model"], record["body"]),
                         ("Tesla", "Model Y", "SUV"))

    def test_a_blank_source_leaves_the_field_to_be_rendered_normally(self):
        # Nothing to follow is not the same as a broken rule, and an optional
        # section nobody filled in must not take its dependents down with it.
        schema = spec(
            {"name": "tier", "control": "select", "options": ["Gold", "Bronze"]},
            {"name": "deductible", "control": "select", "options": ["500", "2500"],
             "follows": "tier", "when": {"Gold": "500", "Bronze": "2500"}},
        )
        records = generate(schema, Options(count=60, seed=7, blank_rate=0.9)).records
        blanked = [r for r in records if r["tier"] == ""]
        self.assertTrue(blanked, "the run produced no blank source to test")
        self.assertEqual(coherence_report(schema, records), [])

    def test_a_combination_the_table_does_not_cover_falls_through(self):
        schema = spec(
            {"name": "tier", "control": "select", "required": True,
             "options": ["Gold", "Bronze"]},
            {"name": "extra", "control": "select", "options": ["Yes", "No"],
             "follows": "tier", "when": {"Gold": "Yes"}},
        )
        records = generate(schema, Options(count=60, seed=9, blank_rate=0)).records
        self.assertEqual({r["extra"] for r in records if r["tier"] == "Gold"}, {"Yes"})
        # Bronze is not in the table, so it is left to the ordinary render and
        # may be either. That is a fall-through, not a broken rule.
        self.assertEqual(coherence_report(schema, records), [])

    def test_a_rule_written_in_the_business_s_words_finds_the_option(self):
        schema = spec(
            {"name": "state", "control": "select", "required": True, "options": ["TX"]},
            {"name": "region", "control": "select",
             "options": [{"value": "SW", "label": "South West"}],
             "follows": "state", "when": {"TX": "South West"}},
        )
        record = generate(schema, Options(count=1, seed=1, blank_rate=0)).records[0]
        self.assertEqual(record["region"], "SW")


class TestARuleThatIsWrong(unittest.TestCase):
    """A rule the generator could not apply is reported, never hidden.

    The failure mode that matters is the quiet one: a dataset that looks
    structured, is not, and sends somebody off to blame the model.
    """

    def test_a_source_the_form_does_not_ask_for_is_named(self):
        schema = spec({"name": "x", "control": "select", "options": ["a"],
                       "follows": "nowhere", "when": {"q": "a"}})
        records = generate(schema, Options(count=3, seed=1)).records
        problems = coherence_report(schema, records)
        self.assertTrue(any("nowhere" in p and "does not ask for" in p
                            for p in problems), problems)

    def test_a_value_that_is_not_one_of_the_options_is_reported_not_swapped(self):
        schema = spec(
            {"name": "tier", "control": "select", "required": True, "options": ["Gold"]},
            {"name": "d", "control": "select", "options": ["250", "500"],
             "follows": "tier", "when": {"Gold": "999"}},
        )
        records = generate(schema, Options(count=4, seed=1, blank_rate=0)).records
        # Not quietly replaced with a random option, which would look fine and
        # be exactly the noise the rule was written to remove.
        self.assertEqual({r["d"] for r in records}, {"999"})
        self.assertTrue(any("not one of its options" in p
                            for p in validate(schema, records)))

    def test_fields_that_follow_each_other_in_a_circle_do_not_hang(self):
        schema = spec(
            {"name": "a", "control": "select", "options": ["1"],
             "follows": "b", "when": {"1": "1"}},
            {"name": "b", "control": "select", "options": ["1"],
             "follows": "a", "when": {"1": "1"}},
        )
        records = generate(schema, Options(count=3, seed=1)).records
        self.assertEqual(len(records), 3)

    def test_a_broken_rule_is_reported_once_not_once_per_record(self):
        schema = spec({"name": "x", "control": "select", "options": ["a"],
                       "follows": "nowhere", "when": {"q": "a"}})
        records = generate(schema, Options(count=500, seed=1)).records
        self.assertEqual(len(coherence_report(schema, records)), 1)


class TestTheBundledFormsBuiltOnRules(unittest.TestCase):
    """The two examples that exist to show what the tool is for.

    Their numbers are the claim the README makes, so they are checked here
    rather than trusted. The thresholds are deliberately well under what the
    forms currently reach: this is a guard against the rules silently
    ceasing to apply, not a pin on an exact score.
    """

    forms = ("auto_insurance_quote", "employee_onboarding")

    def schema(self, name):
        return fillerai.extract_spec(EXAMPLES / f"{name}.fields.json")

    def test_they_generate_data_the_form_itself_would_accept(self):
        for name in self.forms:
            with self.subTest(name):
                schema = self.schema(name)
                records = generate(schema, Options(count=60, seed=4)).records
                self.assertEqual(validate(schema, records), [])

    def test_every_rule_they_declare_actually_holds(self):
        for name in self.forms:
            with self.subTest(name):
                schema = self.schema(name)
                records = generate(schema, Options(count=60, seed=4)).records
                self.assertEqual(coherence_report(schema, records), [])

    def test_most_of_their_fields_carry_a_declared_rule(self):
        for name in self.forms:
            with self.subTest(name):
                schema = self.schema(name)
                ruled = [f for f in schema.fields if f.derived]
                self.assertGreaterEqual(len(ruled), 25)

    def test_a_model_trained_on_them_fills_far_more_than_the_address(self):
        for name, floor in (("auto_insurance_quote", 20), ("employee_onboarding", 25)):
            with self.subTest(name):
                schema = self.schema(name)
                records = generate(schema, Options(count=400, seed=4)).records
                options = TrainOptions(algorithm="statistical", holdout=0.25, seed=1)
                _, holdout = split_records(records, options)
                model = train(schema, records, options)
                learned = [r for r in model.field_report()
                           if r["how"] not in ("you", "usual")]
                self.assertGreaterEqual(len(learned), floor)
                # And it is right when it does fill, which is the half of the
                # claim that a coverage number on its own would hide.
                seeds = suggest_seed_fields(model, 3)
                scored = evaluate(model, holdout, seeds=seeds).to_dict()
                self.assertGreater(scored["accepted_accuracy"], 0.85)


if __name__ == "__main__":
    unittest.main()
