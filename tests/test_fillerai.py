"""Tests for FillerAI. Run with: python -m unittest discover -s tests"""

from __future__ import annotations

import datetime as dt
import json
import io
import random
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.extract import dom, html_form, spec
from fillerai.generate import catalogs
from fillerai.generate.dataset import Options, coherence_report, generate, validate
from fillerai.generate.persona import Persona, _luhn_check_digit
from fillerai.generate.render import compatible_prefixes, party_key, render
from fillerai.infer import infer, infer_field
from fillerai.schema import Constraints, Field, FormSchema, Option

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def make_field(**kwargs) -> Field:
    field = Field(name=kwargs.pop("name", "f"), **{
        k: v for k, v in kwargs.items() if k in Field.__dataclass_fields__
    })
    return field


class TestDom(unittest.TestCase):
    def test_wrapping_label_excludes_its_own_control(self):
        root = dom.parse('<label>City <input name="city"></label>')
        label = next(root.elements("label"))
        self.assertEqual(label.inner_text(), "City")

    def test_unclosed_tags_still_parse(self):
        root = dom.parse("<form><p>one<p>two<input name=a></form>")
        self.assertEqual(len(list(root.elements("input"))), 1)

    def test_valueless_attribute_is_empty_string(self):
        root = dom.parse('<input name="a" required>')
        node = next(root.elements("input"))
        self.assertTrue(node.has("required"))
        self.assertEqual(node.get("required"), "")

    def test_stray_close_tag_is_ignored(self):
        root = dom.parse("</div><form><input name=a></form>")
        self.assertEqual(len(list(root.elements("input"))), 1)


class TestExtraction(unittest.TestCase):
    def test_skips_buttons_and_hidden_fields(self):
        schema = html_form.extract(
            '<form><input name="a"><input type="hidden" name="csrf">'
            '<input type="submit" value="Go"><button>x</button></form>'
        )
        self.assertEqual([f.name for f in schema.fields], ["a"])

    def test_radio_group_becomes_one_field_with_options(self):
        schema = html_form.extract(
            '<fieldset><legend>Contact by</legend>'
            '<label for="a">Email</label><input type="radio" id="a" name="how" value="email" required>'
            '<label for="b">Phone</label><input type="radio" id="b" name="how" value="phone">'
            "</fieldset>"
        )
        self.assertEqual(len(schema.fields), 1)
        field = schema.fields[0]
        self.assertEqual(field.control, "radio")
        self.assertEqual([o.value for o in field.options], ["email", "phone"])
        self.assertEqual([o.label for o in field.options], ["Email", "Phone"])
        self.assertTrue(field.constraints.required)
        self.assertEqual(field.label, "Contact by")

    def test_select_drops_placeholder_option(self):
        schema = html_form.extract(
            '<select name="s"><option value="">-- Select --</option>'
            '<option value="CA">California</option></select>'
        )
        self.assertEqual([o.value for o in schema.fields[0].options], ["CA"])

    def test_constraints_are_read_off_the_control(self):
        schema = html_form.extract(
            '<input name="z" maxlength="10" minlength="5" pattern="\\d+" required>'
            '<input type="number" name="n" min="1" max="9" step="2">'
        )
        text, number = schema.fields
        self.assertEqual(text.constraints.max_length, 10)
        self.assertEqual(text.constraints.min_length, 5)
        self.assertEqual(text.constraints.pattern, "\\d+")
        self.assertTrue(text.constraints.required)
        self.assertEqual((number.constraints.minimum, number.constraints.maximum), (1.0, 9.0))
        self.assertEqual(number.constraints.step, 2.0)

    def test_date_bounds_are_not_read_as_numeric_bounds(self):
        schema = html_form.extract('<input type="date" name="d" min="2020-01-01" max="2020-12-31">')
        field = schema.fields[0]
        self.assertIsNone(field.constraints.minimum)
        self.assertEqual(field.extra["date_min"], "2020-01-01")

    def test_screens_and_groups_are_detected(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        self.assertEqual(
            [s.id for s in schema.screens],
            ["claimant", "addresses", "policy", "incident"],
        )
        self.assertEqual(schema.field("home_city").group, "home")
        self.assertEqual(schema.field("employer_city").group, "employer")
        self.assertEqual(schema.field("home_city").screen, "addresses")

    def test_duplicate_names_are_made_unique(self):
        schema = html_form.extract('<input name="a"><input name="a">')
        self.assertEqual([f.name for f in schema.fields], ["a", "a_2"])

    def test_label_falls_back_through_for_wrapping_and_aria(self):
        schema = html_form.extract(
            '<label for="x">By For</label><input id="x" name="x">'
            '<label>Wrapping <input name="y"></label>'
            '<input name="z" aria-label="By Aria">'
        )
        self.assertEqual([f.label for f in schema.fields], ["By For", "Wrapping", "By Aria"])

    def test_help_text_via_aria_describedby(self):
        schema = html_form.extract(
            '<input name="s" aria-describedby="h"><small id="h">Only for verification.</small>'
        )
        self.assertEqual(schema.fields[0].help_text, "Only for verification.")


class TestInference(unittest.TestCase):
    def infer_one(self, **kwargs) -> Field:
        return infer_field(make_field(**kwargs))

    def test_autocomplete_outranks_a_misleading_name(self):
        field = self.infer_one(name="field_17", extra={"autocomplete": "postal-code"})
        self.assertEqual(field.semantic_type, "postal_code")
        self.assertGreater(field.confidence, 0.9)

    def test_group_prefix_does_not_hijack_the_meaning(self):
        # "employer_city" is a city, not a company.
        field = self.infer_one(name="employer_city", group="employer", label="City")
        self.assertEqual(field.semantic_type, "city")

    def test_specific_name_beats_general_control_type(self):
        field = self.infer_one(name="date_of_birth", control="date")
        self.assertEqual(field.semantic_type, "date_of_birth")
        field = self.infer_one(name="mobile_phone", control="tel")
        self.assertEqual(field.semantic_type, "phone_mobile")

    def test_name_rule_beats_a_looser_label_rule(self):
        field = self.infer_one(name="effective_date", label="Policy Effective Date")
        self.assertEqual(field.semantic_type, "date")

    def test_routing_number_is_not_mistaken_for_a_tax_id(self):
        field = self.infer_one(name="payee_routing_number", group="payee")
        self.assertEqual(field.semantic_type, "routing_number")

    def test_company_name_does_not_fall_through_to_full_name(self):
        self.assertEqual(self.infer_one(name="company_name").semantic_type, "company")

    def test_state_recognised_from_its_option_values(self):
        options = [Option(value=c) for c in ("CA", "NY", "TX", "FL", "WA")]
        self.assertEqual(self.infer_one(name="f_22", options=options).semantic_type, "state")

    def test_unknown_field_is_honest_about_it(self):
        field = self.infer_one(name="xyzzy")
        self.assertEqual(field.semantic_type, "unknown")
        self.assertEqual(field.confidence, 0.0)

    def test_evidence_is_always_recorded(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        for field in schema.fields:
            self.assertTrue(field.evidence, f"{field.name} has no evidence")

    def test_example_form_is_fully_understood(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        low = [f.name for f in schema.fields if f.confidence < 0.7]
        self.assertEqual(low, [], "fields fell below the review threshold")

    def test_data_types_follow_semantics(self):
        self.assertEqual(self.infer_one(name="dob", control="date").data_type, "date")
        self.assertEqual(self.infer_one(name="total_amount").data_type, "number")
        self.assertEqual(self.infer_one(name="age").data_type, "integer")


class TestPersona(unittest.TestCase):
    def test_same_seed_gives_the_same_person(self):
        a, b = Persona(rng=random.Random(5)), Persona(rng=random.Random(5))
        self.assertEqual(a.full_name, b.full_name)
        self.assertEqual(a.address("billing").postal_code, b.address("billing").postal_code)

    def test_address_parts_belong_together(self):
        for seed in range(60):
            address = Persona(rng=random.Random(seed)).address()
            self.assertIn(address.postal_code, address.place.zip_codes)
            self.assertIn(address.area_code, address.place.area_codes)
            self.assertEqual(address.state_name, catalogs.STATE_NAMES[address.state])

    def test_groups_are_independent_but_stable(self):
        persona = Persona(rng=random.Random(11))
        billing = persona.address("billing")
        self.assertIs(persona.address("billing"), billing)
        self.assertIsNot(persona.address("shipping"), billing)

    def test_phone_area_code_matches_the_address(self):
        for seed in range(40):
            persona = Persona(rng=random.Random(seed))
            phone = persona.phone(group="home")
            self.assertEqual(phone.split("-")[0], persona.address("home").area_code)

    def test_email_is_built_from_the_name(self):
        for seed in range(40):
            persona = Persona(rng=random.Random(seed))
            local = persona.email().split("@")[0].lower()
            surname = re.sub(r"[^a-z]", "", persona.last_name.lower())
            self.assertIn(surname, local)

    def test_age_agrees_with_date_of_birth(self):
        for seed in range(60):
            persona = Persona(rng=random.Random(seed))
            born, today = persona.date_of_birth, persona.today
            expected = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day)
            )
            self.assertEqual(persona.age, expected)
            self.assertGreaterEqual(persona.age, 18)

    def test_age_is_exact_on_awkward_reference_dates(self):
        # Regression: picking a birth *year* made someone born in December of
        # "this year minus 18" come out as 17. Leap days and year ends are
        # where that shows up.
        for today in (dt.date(2026, 9, 20), dt.date(2024, 2, 29),
                      dt.date(2026, 1, 1), dt.date(2026, 12, 31)):
            for seed in range(120):
                persona = Persona(rng=random.Random(seed), today=today)
                born = persona.date_of_birth
                expected = today.year - born.year - (
                    (today.month, today.day) < (born.month, born.day)
                )
                self.assertEqual(persona.age, expected, f"{today} seed {seed}")
                self.assertGreaterEqual(persona.age, 18, f"{today} seed {seed}")

    def test_card_number_passes_luhn(self):
        for seed in range(40):
            number = Persona(rng=random.Random(seed)).card("number")
            self.assertEqual(_luhn_check_digit(number[:-1]), number[-1])

    def test_routing_number_passes_the_aba_checksum(self):
        for seed in range(40):
            digits = [int(c) for c in Persona(rng=random.Random(seed)).identifier("routing_number")]
            weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
            self.assertEqual(sum(d * w for d, w in zip(digits, weights)) % 10, 0)

    def test_iban_passes_mod_97(self):
        for seed in range(20):
            iban = Persona(rng=random.Random(seed)).identifier("iban")
            rotated = iban[4:] + iban[:4]
            numeric = "".join(str(int(c, 36)) for c in rotated)
            self.assertEqual(int(numeric) % 97, 1)

    def test_safe_identifiers_cannot_belong_to_a_real_person(self):
        for seed in range(50):
            persona = Persona(rng=random.Random(seed))
            area = int(persona.identifier("ssn")[:3])
            self.assertGreaterEqual(area, 900, "SSA has never issued this range")
            self.assertRegex(persona.phone(), r"^\d{3}-555-01\d{2}$")
            self.assertIn(persona.email().split("@")[1], catalogs.EMAIL_DOMAINS)

    def test_state_constraint_is_respected(self):
        for seed in range(30):
            persona = Persona(rng=random.Random(seed))
            persona.constrain_states("billing", ("CA", "TX"))
            self.assertIn(persona.address("billing").state, ("CA", "TX"))

    def test_every_us_state_is_represented(self):
        self.assertEqual(len({p.state for p in catalogs.PLACES}), 51)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(3)
        self.persona = Persona(rng=random.Random(3))

    def test_value_comes_from_the_option_list(self):
        field = make_field(name="state", semantic_type="state",
                           control="select",
                           options=[Option(value="CA", label="California"),
                                    Option(value="NY", label="New York")])
        self.persona.constrain_states(None, ("CA", "NY"))
        self.assertIn(render(field, self.persona, self.rng), {"CA", "NY"})

    def test_full_state_name_matched_to_a_coded_option(self):
        field = make_field(name="state", semantic_type="state", control="select",
                           options=[Option(value="CA", label="California")])
        self.persona.constrain_states(None, ("CA",))
        self.assertEqual(render(field, self.persona, self.rng), "CA")

    def test_maxlength_is_never_exceeded(self):
        field = make_field(name="notes", semantic_type="free_text", control="textarea",
                           constraints=Constraints(max_length=25))
        self.assertLessEqual(len(render(field, self.persona, self.rng)), 25)

    def test_minlength_is_met(self):
        field = make_field(name="notes", semantic_type="free_text", control="textarea",
                           constraints=Constraints(min_length=200, max_length=400))
        self.assertGreaterEqual(len(render(field, self.persona, self.rng)), 200)

    def test_native_date_control_gets_iso(self):
        field = make_field(name="dob", semantic_type="date_of_birth", control="date")
        self.assertRegex(render(field, self.persona, self.rng), r"^\d{4}-\d{2}-\d{2}$")

    def test_placeholder_dictates_the_date_format(self):
        field = make_field(name="dob", semantic_type="date_of_birth",
                           control="text", placeholder="MM/DD/YYYY")
        self.assertRegex(render(field, self.persona, self.rng), r"^\d{2}/\d{2}/\d{4}$")

    def test_phone_format_follows_the_placeholder(self):
        field = make_field(name="phone", semantic_type="phone",
                           control="tel", placeholder="(555) 555-0100")
        self.assertRegex(render(field, self.persona, self.rng), r"^\(\d{3}\) \d{3}-\d{4}$")

    def test_ssn_without_separators_when_the_box_holds_nine(self):
        field = make_field(name="ssn", semantic_type="ssn",
                           constraints=Constraints(max_length=9))
        self.assertRegex(render(field, self.persona, self.rng), r"^\d{9}$")

    def test_middle_initial_is_a_single_character(self):
        field = make_field(name="mi", semantic_type="middle_name",
                           constraints=Constraints(max_length=1))
        self.assertLessEqual(len(render(field, self.persona, self.rng)), 1)

    def test_number_respects_bounds_and_step(self):
        field = make_field(name="d", semantic_type="decimal", control="number",
                           data_type="integer",
                           constraints=Constraints(minimum=0, maximum=5000, step=50))
        for _ in range(50):
            value = int(render(field, self.persona, self.rng))
            self.assertTrue(0 <= value <= 5000)
            self.assertEqual(value % 50, 0)

    def test_expiry_date_lands_in_the_future(self):
        field = make_field(name="renewal_date", semantic_type="date", control="date")
        value = dt.date.fromisoformat(render(field, self.persona, self.rng))
        self.assertGreaterEqual(value, self.persona.today)

    def test_date_bounds_from_the_control_are_honoured(self):
        field = make_field(name="d", semantic_type="date", control="date",
                           extra={"date_min": "2021-01-01", "date_max": "2021-12-31"})
        for _ in range(25):
            value = dt.date.fromisoformat(render(field, self.persona, self.rng))
            self.assertEqual(value.year, 2021)


class TestGenderCoherence(unittest.TestCase):
    def test_persona_title_agrees_with_its_gender(self):
        for seed in range(200):
            persona = Persona(rng=random.Random(seed))
            if persona.prefix:
                self.assertIn(persona.prefix, catalogs.PREFIXES_BY_GENDER[persona.gender])

    def test_title_and_gender_agree_across_a_dataset(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        for seed in (42, 101, 2024):
            dataset = generate(schema, Options(count=100, seed=seed, blank_rate=0.0))
            self.assertEqual(coherence_report(schema, dataset.records), [])

    def test_no_compatible_title_leaves_the_field_blank(self):
        # A form offering only "Mr." cannot title a non-binary persona, and a
        # blank optional field beats a contradiction.
        field = make_field(name="prefix", semantic_type="prefix", control="select",
                           options=[Option(value="Mr.")])
        persona = Persona(rng=random.Random(1))
        persona.gender = "Non-binary"
        self.assertEqual(compatible_prefixes(field, "Non-binary"), [])
        self.assertEqual(render(field, persona, random.Random(1)), "")

    def test_title_is_taken_from_the_forms_own_options(self):
        field = make_field(name="prefix", semantic_type="prefix", control="select",
                           options=[Option(value="Ms."), Option(value="Mrs.")])
        persona = Persona(rng=random.Random(1))
        persona.gender = "Female"
        persona.prefix = "Prof."  # not on this form's list
        self.assertIn(render(field, persona, random.Random(1)), {"Ms.", "Mrs."})

    def test_mismatched_title_is_reported(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        record = dict(generate(schema, Options(count=1, seed=3, blank_rate=0.0)).records[0])
        record["gender"], record["prefix"] = "Male", "Mrs."
        self.assertTrue(any("does not agree" in p for p in coherence_report(schema, [record])))


class TestDistinctParties(unittest.TestCase):
    def test_party_key_detects_another_person(self):
        self.assertEqual(party_key(make_field(name="referring_provider")), "referring")
        self.assertEqual(party_key(make_field(name="full_name", group="spouse")), "spouse")

    def test_an_address_group_is_not_another_person(self):
        self.assertIsNone(party_key(make_field(name="shipping_city", group="shipping")))
        self.assertIsNone(party_key(make_field(name="home_city", group="home")))

    def test_related_person_is_not_the_subject(self):
        schema = fillerai.extract_spec(EXAMPLES / "patient_registration.fields.json")
        dataset = generate(schema, Options(count=20, seed=4, blank_rate=0.0))
        same = 0
        for record in dataset.records:
            patient = f"{record['patient_first_name']} {record['patient_last_name']}"
            if record["referring_provider"].startswith(patient):
                same += 1
        self.assertEqual(same, 0, "the referring provider is the patient")

    def test_related_person_is_stable_within_a_record(self):
        persona = Persona(rng=random.Random(2))
        self.assertIs(persona.related("spouse"), persona.related("spouse"))
        self.assertIsNot(persona.related("spouse"), persona.related("beneficiary"))

    def test_related_person_is_reproducible_from_the_seed(self):
        a = Persona(rng=random.Random(9)).related("spouse").full_name
        b = Persona(rng=random.Random(9)).related("spouse").full_name
        self.assertEqual(a, b)


class TestDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")

    def test_records_are_valid_and_coherent(self):
        for seed in (1, 2, 42, 999):
            dataset = generate(self.schema, Options(count=25, seed=seed))
            self.assertEqual(validate(self.schema, dataset.records), [])
            self.assertEqual(coherence_report(self.schema, dataset.records), [])

    def test_same_seed_reproduces_the_dataset(self):
        first = generate(self.schema, Options(count=5, seed=77)).records
        second = generate(self.schema, Options(count=5, seed=77)).records
        self.assertEqual(first, second)

    def test_different_seeds_differ(self):
        first = generate(self.schema, Options(count=5, seed=1)).records
        second = generate(self.schema, Options(count=5, seed=2)).records
        self.assertNotEqual(first, second)

    def test_no_blanks_when_blank_rate_is_zero(self):
        # Fields whose emptiness is a fact about the person rather than a
        # deliberate blank: most people have no suffix and no middle name.
        naturally_empty = {"home_address_line2", "suffix", "middle_initial", "prefix"}
        dataset = generate(self.schema, Options(count=10, seed=5, blank_rate=0.0))
        for record in dataset.records:
            for name, value in record.items():
                if name in naturally_empty:
                    continue
                self.assertNotEqual(value, "", f"{name} was blank")

    def test_required_checkbox_is_always_ticked(self):
        schema = fillerai.extract_spec(EXAMPLES / "patient_registration.fields.json")
        dataset = generate(schema, Options(count=20, seed=3))
        self.assertEqual(validate(schema, dataset.records), [])
        for record in dataset.records:
            self.assertIs(record["consent_to_treat"], True)

    def test_false_checkbox_is_a_value_not_a_blank(self):
        field = Field(name="opt_in", semantic_type="boolean", control="checkbox")
        schema = FormSchema(name="t", fields=[field])
        self.assertEqual(validate(schema, [{"opt_in": False}]), [])

    def test_readonly_fields_are_left_to_the_form(self):
        dataset = generate(self.schema, Options(count=3, seed=5))
        self.assertNotIn("claim_number", dataset.records[0])

    def test_required_fields_are_always_filled(self):
        dataset = generate(self.schema, Options(count=30, seed=8, blank_rate=0.9))
        required = [f.name for f in self.schema.fields
                    if f.constraints.required and not f.constraints.read_only]
        for record in dataset.records:
            for name in required:
                self.assertNotEqual(record[name], "", f"required {name} was blank")

    def test_partial_address_is_reported(self):
        dataset = generate(self.schema, Options(count=1, seed=3, blank_rate=0.0))
        broken = dict(dataset.records[0])
        broken["home_state"] = ""
        problems = coherence_report(self.schema, broken and [broken])
        self.assertTrue(any("partly filled" in p for p in problems))

    def test_mismatched_city_is_reported(self):
        dataset = generate(self.schema, Options(count=1, seed=3, blank_rate=0.0))
        broken = dict(dataset.records[0])
        broken["home_city"] = "Nowhere"
        self.assertTrue(any("does not belong" in p for p in coherence_report(self.schema, [broken])))

    def test_validate_catches_an_over_long_value(self):
        broken = generate(self.schema, Options(count=1, seed=3)).records[0].copy()
        broken["first_name"] = "x" * 100
        self.assertTrue(any("maxlength" in p for p in validate(self.schema, [broken])))

    def test_csv_has_one_column_per_field(self):
        dataset = generate(self.schema, Options(count=4, seed=6))
        lines = dataset.to_csv().strip().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0].count(",") + 1, len(self.schema.fields))

    def test_ndjson_is_one_object_per_line(self):
        dataset = generate(self.schema, Options(count=4, seed=6))
        rows = [json.loads(line) for line in dataset.to_ndjson().splitlines()]
        self.assertEqual(len(rows), 4)


class TestSchemaFormat(unittest.TestCase):
    def test_schema_round_trips_through_json(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        again = FormSchema.from_json(schema.to_json())
        self.assertEqual(again.to_dict(), schema.to_dict())

    def test_zero_bounds_survive_serialisation(self):
        # Regression: ``0 == False`` in Python, so a falsy-value filter used to
        # drop ``minimum: 0`` and let a reloaded schema generate out-of-range
        # numbers. Zero is a real bound and must round-trip.
        field = Field(name="n", constraints=Constraints(minimum=0, maximum=5000, step=50))
        restored = Field.from_dict(field.to_dict())
        self.assertEqual(restored.constraints.minimum, 0)
        self.assertEqual(restored.constraints.maximum, 5000)

    def test_bounds_still_hold_after_a_round_trip(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        reloaded = FormSchema.from_json(schema.to_json())
        dataset = generate(reloaded, Options(count=20, seed=1))
        self.assertEqual(validate(reloaded, dataset.records), [])

    def test_incompatible_major_version_is_refused(self):
        with self.assertRaises(ValueError):
            FormSchema.from_dict({"schema_version": "9.0", "name": "x", "fields": []})

    def test_unknown_keys_are_ignored_not_fatal(self):
        schema = FormSchema.from_dict({
            "schema_version": "1.0", "name": "x", "future_key": 1,
            "fields": [{"name": "a", "another_future_key": True}],
        })
        self.assertEqual(schema.fields[0].name, "a")

    def test_generation_works_from_a_saved_schema(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.json"
            path.write_text(schema.to_json(), encoding="utf-8")
            reloaded = fillerai.extract_spec(path)
        self.assertEqual(
            [f.semantic_type for f in reloaded.fields],
            [f.semantic_type for f in schema.fields],
        )


class TestSpecLoader(unittest.TestCase):
    def test_flat_constraint_keys_are_accepted(self):
        schema = spec.load({"name": "t", "fields": [
            {"name": "email", "label": "Email", "required": True, "max_length": 80},
        ]})
        self.assertTrue(schema.fields[0].constraints.required)
        self.assertEqual(schema.fields[0].constraints.max_length, 80)

    def test_declared_semantic_type_is_trusted(self):
        schema = infer(spec.load({"name": "t", "fields": [
            {"name": "xyzzy", "semantic_type": "city"},
        ]}))
        self.assertEqual(schema.fields[0].semantic_type, "city")
        self.assertEqual(schema.fields[0].confidence, 1.0)

    def test_plain_string_options_expand(self):
        schema = spec.load({"name": "t", "fields": [
            {"name": "s", "control": "select", "options": ["CA", "NY"]},
        ]})
        self.assertEqual([o.value for o in schema.fields[0].options], ["CA", "NY"])

    def test_undeclared_screen_is_still_registered(self):
        schema = spec.load({"name": "t", "fields": [{"name": "a", "screen": "one"}]})
        self.assertEqual([s.id for s in schema.screens], ["one"])

    def test_missing_fields_key_is_an_error(self):
        with self.assertRaises(ValueError):
            spec.load({"name": "t"})


class TestCli(unittest.TestCase):
    def test_extract_then_generate_then_check(self):
        from fillerai.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = str(Path(tmp) / "schema.json")
            data_path = str(Path(tmp) / "data.json")
            self.assertEqual(main(["extract", str(EXAMPLES / "claims_intake.html"),
                                   "-o", schema_path]), 0)
            self.assertEqual(main(["generate", schema_path, "-n", "5", "--seed", "1",
                                   "-o", data_path, "--check"]), 0)
            self.assertEqual(main(["check", schema_path, data_path]), 0)
            records = json.loads(Path(data_path).read_text())["records"]
            self.assertEqual(len(records), 5)

    def test_algorithms_lists_what_can_be_selected(self):
        from fillerai.cli import main

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["algorithms", "--recipe"]), 0)
        printed = out.getvalue()
        for name in ("statistical", "tree", "forest", "nearest", "bayes"):
            self.assertIn(name, printed)
        self.assertIn("--set", printed)

    def test_training_with_a_chosen_algorithm_writes_a_model_and_a_script(self):
        from fillerai.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = str(Path(tmp) / "schema.json")
            data_path = str(Path(tmp) / "data.json")
            model_path = str(Path(tmp) / "model.json")
            script_path = str(Path(tmp) / "run.py")
            main(["extract", str(EXAMPLES / "claims_intake.html"), "-o", schema_path])
            main(["generate", schema_path, "-n", "80", "--seed", "1", "-o", data_path])

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main([
                    "train", schema_path, data_path, "-a", "tree",
                    "--set", "max_depth=3", "--seed", "1",
                    "-o", model_path, "--script", script_path,
                ]), 0)

            model = json.loads(Path(model_path).read_text())
            self.assertEqual(model["algorithm"], "tree")
            self.assertEqual(model["settings"]["tuning"], {"max_depth": 3})
            # The script it wrote has to run, not merely exist.
            compile(Path(script_path).read_text(), "run.py", "exec")

    def test_an_unknown_setting_value_is_refused_rather_than_guessed(self):
        from fillerai.cli import build_parser, _tuning

        self.assertEqual(_tuning(["trees=16", "rate=0.5", "on=true", "name=x"]),
                         {"trees": 16, "rate": 0.5, "on": True, "name": "x"})
        with self.assertRaises(SystemExit):
            _tuning(["nonsense"])

    def test_compare_puts_every_algorithm_side_by_side(self):
        from fillerai.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = str(Path(tmp) / "schema.json")
            data_path = str(Path(tmp) / "data.json")
            main(["extract", str(EXAMPLES / "claims_intake.html"), "-o", schema_path])
            main(["generate", schema_path, "-n", "80", "--seed", "1", "-o", data_path])

            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                self.assertEqual(main([
                    "train", schema_path, data_path, "--seed", "1",
                    "--compare", "statistical", "tree",
                ]), 0)
            printed = out.getvalue()
            self.assertIn("statistical", printed)
            self.assertIn("tree", printed)
            self.assertIn("best on this form", printed)

    def test_the_library_records_the_chain_from_source_to_model(self):
        from fillerai.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            library = str(Path(tmp) / "library")
            schema_path = str(Path(tmp) / "schema.json")
            data_path = str(Path(tmp) / "data.json")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(["extract", str(EXAMPLES / "claims_intake.html"),
                      "-o", schema_path, "--save", "--library", library])
                main(["generate", schema_path, "-n", "60", "--seed", "1",
                      "-o", data_path, "--save", "--library", library])
                main(["train", schema_path, data_path, "-a", "tree", "--seed", "1",
                      "--save", "--library", library])

                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(
                        main(["library", "--library", library, "list"]), 0)

            listed = out.getvalue()
            for prefix in ("src-", "sch-", "dat-", "mdl-"):
                self.assertIn(prefix, listed)

            from fillerai.store import Store

            store = Store(library)
            model = store.list("model")[0]
            kinds = [entry.kind for entry in store.lineage(model.id)]
            self.assertEqual(kinds, ["schema", "dataset", "model"])

    def test_showing_a_library_entry_names_what_came_of_it(self):
        from fillerai.cli import main
        from fillerai.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            library = str(Path(tmp) / "library")
            schema_path = str(Path(tmp) / "schema.json")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(["extract", str(EXAMPLES / "claims_intake.html"),
                      "-o", schema_path, "--save", "--library", library])
            source = Store(library).list("source")[0]

            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(
                    main(["library", "--library", library, "show", source.id]), 0)
            self.assertIn("used to make", out.getvalue())

    def test_asking_the_library_for_something_that_is_not_there(self):
        from fillerai.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(["library", "--library", tmp,
                                       "show", "../../etc/passwd"]), 1)
            self.assertIn("library id", err.getvalue())


if __name__ == "__main__":
    unittest.main()
