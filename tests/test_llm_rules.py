"""The gauntlet a proposed rule has to survive.

A language model proposing a form's business rules is guessing - from a field
list and from what it knows about the world, but guessing. So the feature is
not the asking, it is everything that happens to the answer afterwards, and
that is what these cover.

Every one runs offline against a recorded exchange. The model is replaced, the
checks are not: what is under test here is ordinary deterministic code that
takes a fixed blob of JSON and decides what to believe.

**The fixture is hand-authored, not recorded.** There was no API key available
when this was written, so ``tests/fixtures/llm/rules_member_enrollment.json``
is a plausible response constructed by hand to drive every branch below. It
says so in the file. It is honest as a test of the gate and it is not evidence
about what a model actually proposes - that needs the live eval in
``livetests/``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fillerai
from fillerai.generate.dataset import Options, coherence_report, generate, validate
from fillerai.llm import rules as llm_rules
from fillerai.llm.client import Client
from fillerai.llm.config import Settings
from fillerai.llm.rules import Proposal
from fillerai.llm.transport import RecordedTransport

FIXTURE = ROOT / "tests" / "fixtures" / "llm" / "rules_member_enrollment.json"
FORM = ROOT / "examples" / "member_enrollment.fields.json"


def proposal(field, follows, when, **rest) -> Proposal:
    return Proposal.from_payload({
        "field": field, "follows": follows, "when": when,
        "confidence": rest.get("confidence", 0.8),
        "why": rest.get("why", "because"),
        "otherwise": rest.get("otherwise"),
    })


class TestStructuralChecks(unittest.TestCase):
    """The checks that need no data, one failure mode each."""

    def setUp(self):
        self.schema = fillerai.extract_spec(FORM)

    def problem(self, p) -> str | None:
        return llm_rules.check(self.schema, [p])[0].problem

    def test_a_good_rule_survives(self):
        self.assertIsNone(self.problem(
            proposal("home_country", ["home_state"], {"CA": "US", "TX": "US"})))

    def test_a_field_that_is_not_on_the_form_is_dropped(self):
        self.assertIn("no field of that name",
                      self.problem(proposal("invented", ["home_state"], {"CA": "US"})))

    def test_following_a_field_that_is_not_on_the_form_is_dropped(self):
        self.assertIn("not on this form",
                      self.problem(proposal("home_country", ["annual_income"],
                                            {"low": "US"})))

    def test_a_field_following_itself_is_dropped(self):
        self.assertIn("follows itself",
                      self.problem(proposal("plan_tier", ["plan_tier"],
                                            {"Gold": "Gold"})))

    def test_naming_the_same_source_twice_is_dropped(self):
        self.assertIn("same field twice",
                      self.problem(proposal("home_country",
                                            ["home_state", "home_state"],
                                            {"CA|CA": "US"})))

    def test_a_value_the_field_cannot_hold_is_dropped(self):
        """The check that matters most: `_as_written` reports, it does not guess."""
        self.assertIn("not one of its options",
                      self.problem(proposal("plan_tier", ["coverage_level"],
                                            {"Family": "Titanium"})))

    def test_a_key_the_source_cannot_hold_is_dropped(self):
        self.assertIn("cannot hold",
                      self.problem(proposal("member_gender", ["member_prefix"],
                                            {"Sir": "Male"})))

    def test_a_dropped_key_is_reported_in_the_spelling_it_was_written_in(self):
        self.assertIn("'Sir'",
                      self.problem(proposal("member_gender", ["member_prefix"],
                                            {"Sir": "Male"})))

    def test_a_key_of_the_wrong_width_is_dropped(self):
        self.assertIn("value(s) for",
                      self.problem(proposal("home_country",
                                            ["home_state", "plan_tier"],
                                            {"CA": "US"})))

    def test_an_empty_rule_is_dropped(self):
        self.assertIn("no values",
                      self.problem(proposal("home_country", ["home_state"], {})))

    def test_a_field_that_already_has_a_rule_is_left_alone(self):
        """A rule somebody wrote by hand outranks one a model proposed."""
        schema = fillerai.extract_spec(ROOT / "examples" / "auto_insurance_quote.fields.json")
        ruled = next(f.name for f in schema.fields if f.derived)
        verdict = llm_rules.check(schema, [
            proposal(ruled, ["vehicle_model"], {"Wrangler": "x"})])[0]
        self.assertIn("already declares a rule", verdict.problem)


class TestCycles(unittest.TestCase):
    def setUp(self):
        self.schema = fillerai.extract_spec(FORM)

    def test_two_rules_that_follow_each_other_are_both_dropped(self):
        verdicts = llm_rules.check(self.schema, [
            proposal("plan_tier", ["coverage_level"], {"Family": "Gold"}),
            proposal("coverage_level", ["plan_tier"], {"Gold": "Family"}),
        ])
        self.assertTrue(all("cycle" in v.problem for v in verdicts), verdicts)

    def test_a_longer_cycle_is_found_too(self):
        verdicts = llm_rules.check(self.schema, [
            proposal("plan_tier", ["coverage_level"], {"Family": "Gold"}),
            proposal("coverage_level", ["member_gender"], {"Male": "Family"}),
            proposal("member_gender", ["plan_tier"], {"Gold": "Male"}),
        ])
        self.assertEqual(sum("cycle" in v.problem for v in verdicts), 3)

    def test_a_chain_that_is_not_a_cycle_is_fine(self):
        verdicts = llm_rules.check(self.schema, [
            proposal("home_country", ["home_state"], {"CA": "US", "TX": "US"}),
        ])
        self.assertTrue(verdicts[0].kept, verdicts[0].problem)


class TestTheGeneratedDataCheck(unittest.TestCase):
    """The only check that can catch a rule that is well-formed and wrong."""

    def setUp(self):
        self.schema = fillerai.extract_spec(FORM)

    def test_a_rule_that_contradicts_the_data_is_dropped(self):
        """Every title mapped to the wrong gender: well-formed, and nonsense."""
        verdict = llm_rules.check(self.schema, [
            proposal("member_prefix", ["member_gender"],
                     {"Female": "Mr.", "Male": "Mrs.",
                      "Non-binary": "Mrs.", "Prefer not to say": "Mr."}),
        ], sample=80)[0]
        self.assertFalse(verdict.kept)
        self.assertIn("generated data", verdict.problem)

    def test_a_sensible_rule_leaves_the_data_coherent(self):
        verdict = llm_rules.check(self.schema, [
            proposal("member_prefix", ["member_gender"],
                     {"Female": ["Ms.", "Mrs."], "Male": ["Mr."],
                      "Non-binary": ["Mx."]}),
        ], sample=80)[0]
        self.assertTrue(verdict.kept, verdict.problem)

    def test_rules_that_pass_actually_hold_in_the_records(self):
        """Not just 'no new problems' - the rule is visible in the data."""
        kept = [proposal("home_country", ["home_state"],
                         {s: "US" for s in
                          ("AZ", "CA", "CO", "NV", "NM", "OR", "TX", "UT", "WA")})]
        self.assertTrue(llm_rules.check(self.schema, kept, sample=60)[0].kept)
        ruled = llm_rules._with_rules(self.schema, kept)
        records = [dict(r) for r in generate(ruled, Options(count=60, seed=5)).records]
        pairs = {(r["home_state"], r["home_country"]) for r in records
                 if r["home_state"] and r["home_country"]}
        self.assertTrue(all(country == "US" for _, country in pairs), pairs)
        self.assertEqual(validate(ruled, records) + coherence_report(ruled, records), [])


class TestParsing(unittest.TestCase):
    def test_a_rule_with_nothing_to_follow_is_reported_not_crashed_on(self):
        proposals, unreadable = llm_rules.parse(
            {"rules": [{"field": "x", "follows": [], "when": {}}]})
        self.assertEqual(proposals, [])
        self.assertEqual(len(unreadable), 1)

    def test_a_rule_with_no_field_name_is_reported(self):
        _, unreadable = llm_rules.parse({"rules": [{"follows": ["a"], "when": {"1": "2"}}]})
        self.assertIn("no field name", unreadable[0])

    def test_an_answer_with_no_rules_list_is_refused(self):
        with self.assertRaises(ValueError):
            llm_rules.parse({"something_else": []})

    def test_a_rule_that_is_not_an_object_is_reported(self):
        _, unreadable = llm_rules.parse({"rules": ["a string"]})
        self.assertEqual(len(unreadable), 1)


class TestAgainstTheFixture(unittest.TestCase):
    """End to end, with the network replaced by a recording."""

    def setUp(self):
        self.schema = fillerai.extract_spec(FORM)
        self.settings = Settings.resolve(
            "rules", environ={"FILLERAI_LLM_KEY": "not-a-real-key"})
        self.transport = RecordedTransport.from_path(FIXTURE)
        self.client = Client(self.settings, self.transport)

    def run_it(self):
        return llm_rules.propose(self.schema, client=self.client,
                                 settings=self.settings, sample=80)

    def test_the_good_rules_are_kept_and_the_rest_explained(self):
        proposals = self.run_it()
        kept = {p.field for p in proposals.kept}
        self.assertEqual(
            kept, {"member_prefix", "dependent_relationship", "home_country"})
        self.assertTrue(all(v.problem for v in proposals.dropped))

    def test_every_dropped_rule_says_which_check_killed_it(self):
        dropped = {v.proposal.field: v.problem for v in self.run_it().dropped}
        self.assertIn("not one of its options", dropped["plan_tier"])
        self.assertIn("not on this form", dropped["monthly_premium"])
        self.assertIn("cannot hold", dropped["member_gender"])
        self.assertIn("follows itself", dropped["group_number"])

    def test_a_rule_that_could_not_be_parsed_still_appears_in_the_report(self):
        problems = " ".join(v.problem for v in self.run_it().dropped)
        self.assertIn("effective_date", problems)

    def test_the_request_asked_for_structured_output(self):
        self.run_it()
        sent = self.transport.sent[0]
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertIn("member_prefix", sent["messages"][0]["content"])

    def test_the_report_marks_low_confidence_rules_for_review(self):
        lines = self.run_it().report(review_below=0.9)
        self.assertTrue(any(line.strip().startswith("?") for line in lines))
        self.assertTrue(any("worth a human look" in line for line in lines))

    def test_the_written_form_round_trips_through_the_spec_loader(self):
        """What propose-rules writes has to be what apply-rules can read."""
        written = json.loads(json.dumps(self.run_it().to_dict()))
        for entry in written["kept"]:
            again = Proposal.from_payload(entry)
            self.assertTrue(again.sources)

    def test_an_unrecorded_second_call_raises_rather_than_reaching_out(self):
        self.run_it()
        with self.assertRaises(Exception):
            self.run_it()


class TestApply(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(FORM.read_text(encoding="utf-8"))
        self.proposal = proposal("home_country", ["home_state"], {"CA": "US"})

    def test_a_rule_lands_on_the_right_field(self):
        merged, skipped = llm_rules.apply(self.spec, [self.proposal])
        entry = next(f for f in merged["fields"] if f["name"] == "home_country")
        self.assertEqual(entry["follows"], "home_state")
        self.assertEqual(skipped, [])

    def test_the_original_spec_is_not_touched(self):
        llm_rules.apply(self.spec, [self.proposal])
        entry = next(f for f in self.spec["fields"] if f["name"] == "home_country")
        self.assertNotIn("follows", entry)

    def test_a_hand_written_rule_is_never_overwritten(self):
        spec = json.loads(json.dumps(self.spec))
        entry = next(f for f in spec["fields"] if f["name"] == "home_country")
        entry["follows"] = "something_else"
        merged, skipped = llm_rules.apply(spec, [self.proposal])
        kept = next(f for f in merged["fields"] if f["name"] == "home_country")
        self.assertEqual(kept["follows"], "something_else")
        self.assertIn("already declares a rule", skipped[0])

    def test_a_field_the_spec_does_not_have_is_reported(self):
        stray = proposal("not_on_this_spec", ["home_state"], {"CA": "US"})
        _, skipped = llm_rules.apply(self.spec, [stray])
        self.assertIn("not in this spec", skipped[0])

    def test_a_file_that_is_not_a_spec_is_refused(self):
        with self.assertRaises(ValueError):
            llm_rules.apply({"schema_version": 3}, [self.proposal])

    def test_what_apply_writes_loads_back_as_a_schema_with_the_rule_on_it(self):
        merged, _ = llm_rules.apply(self.spec, [self.proposal])
        schema = fillerai.extract.spec.load(merged)
        field = next(f for f in schema.fields if f.name == "home_country")
        self.assertIsNotNone(field.derived)
        self.assertEqual(field.derived.sources, ("home_state",))


if __name__ == "__main__":
    unittest.main()
