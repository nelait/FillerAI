"""Tests for reading, mapping and cleaning real records.

The property everything else rests on: a generated dataset written out and
read back through the cleaner comes back exactly as it went in, in every
file format. If that holds, a model trained on invented records can be
scored on real ones and the two speak the same language.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai import extract_html, extract_spec, generate, infer
from fillerai import realdata
from fillerai.extract import spec as spec_loader
from fillerai.realdata import DataError, cell, clean, read_table, suggest_mapping
from fillerai.schema import Constraints, Field, FormSchema, Option

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def form() -> FormSchema:
    """A small form with one field of every kind the cleaner treats apart."""
    return FormSchema(name="intake", fields=[
        Field(name="first_name", label="First Name", semantic_type="first_name"),
        Field(name="email", label="Email", semantic_type="email"),
        Field(name="state", label="State", semantic_type="state", control="select",
              options=[Option("CA", "California"), Option("NY", "New York"),
                       Option("N/A", "Not applicable")]),
        Field(name="amount", label="Claim Amount", semantic_type="currency_amount",
              data_type="number", control="number",
              constraints=Constraints(minimum=0, maximum=10000)),
        Field(name="visits", label="Visits", semantic_type="integer",
              data_type="integer", control="number"),
        Field(name="loss_date", label="Date of Loss", semantic_type="date",
              data_type="date", control="date"),
        Field(name="police", label="Police report", semantic_type="boolean",
              data_type="boolean", control="checkbox"),
        Field(name="member_id", label="Member ID", semantic_type="member_id",
              constraints=Constraints(required=True)),
        Field(name="claim_number", label="Claim Number", semantic_type="claim_number",
              constraints=Constraints(read_only=True)),
    ])


def to_csv(rows: list[dict], delimiter: str = ",") -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), delimiter=delimiter)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


class TestReading(unittest.TestCase):
    def test_csv_with_any_common_delimiter(self):
        for delimiter in (",", ";", "\t", "|"):
            table = read_table(to_csv([{"a": "1", "b": "x y"}, {"a": "2", "b": "z"}],
                                      delimiter), "f.csv")
            self.assertEqual(table.columns, ["a", "b"], delimiter)
            self.assertEqual(table.rows[1], {"a": "2", "b": "z"})

    def test_byte_order_mark_and_blank_lines(self):
        table = read_table("﻿name,age\n\nAnn,30\n,\n", "f.csv")
        self.assertEqual(table.columns, ["name", "age"])
        self.assertEqual(table.rows, [{"name": "Ann", "age": "30"}])

    def test_blank_and_repeated_headers_still_name_a_column(self):
        table = read_table("a,,a\n1,2,3\n", "f.csv")
        self.assertEqual(table.columns, ["a", "column_2", "a_2"])

    def test_short_rows_are_padded(self):
        table = read_table("a,b,c\n1\n", "f.csv")
        self.assertEqual(table.rows[0], {"a": "1", "b": "", "c": ""})

    def test_json_list_object_and_ndjson(self):
        rows = [{"a": 1, "b": True, "c": ["x", "y"], "_persona": {}}, {"a": None}]
        for text, name in ((json.dumps(rows), "f.json"),
                           (json.dumps({"records": rows}), "f.json"),
                           ("\n".join(json.dumps(r) for r in rows), "f.ndjson"),
                           (json.dumps(rows), "")):
            table = read_table(text, name)
            self.assertEqual(table.columns, ["a", "b", "c"], name)
            self.assertEqual(table.rows[0], {"a": "1", "b": "true", "c": "x|y"})

    def test_what_cannot_be_read_says_why(self):
        for text, name, words in (("", "f.csv", "empty"),
                                  ("[1, 2]", "f.json", "not an object"),
                                  ('{"x": 1', "f.json", "not valid JSON"),
                                  ('{"rows": 3}', "f.json", "list of records")):
            with self.assertRaises(DataError) as caught:
                read_table(text, name)
            self.assertIn(words, str(caught.exception))


class TestMapping(unittest.TestCase):
    def test_names_labels_and_near_misses(self):
        mapping = suggest_mapping(form(), [
            "First Name", "E-mail", "state", "Claim Amt", "Date of Loss",
            "Unrelated", "Claim Number",
        ])
        self.assertEqual(mapping["First Name"], "first_name")
        self.assertEqual(mapping["E-mail"], "email")
        self.assertEqual(mapping["state"], "state")
        self.assertEqual(mapping["Date of Loss"], "loss_date")
        self.assertEqual(mapping["Unrelated"], "")
        # A read-only field is filled by the system, never by a record.
        self.assertEqual(mapping["Claim Number"], "")

    def test_an_exact_match_beats_an_earlier_near_one(self):
        mapping = suggest_mapping(form(), ["visit", "visits"])
        self.assertEqual(mapping["visits"], "visits")
        self.assertEqual(mapping["visit"], "")

    def test_a_field_twice_or_one_the_form_lacks_is_refused(self):
        table = read_table("a,b\n1,2\n", "f.csv")
        with self.assertRaises(DataError):
            clean(form(), table, {"a": "email", "b": "email"})
        with self.assertRaises(DataError):
            clean(form(), table, {"a": "nope"})
        with self.assertRaises(DataError):
            clean(form(), table, {"a": "", "b": ""})


class TestFixes(unittest.TestCase):
    def run_clean(self, rows, fixes=None):
        table = read_table(to_csv(rows), "f.csv")
        return clean(form(), table, None, fixes)

    def row(self, **values):
        base = {"First Name": "Ann", "Email": "ann@example.com", "State": "CA",
                "Claim Amount": "100.00", "Visits": "2", "Date of Loss": "2024-03-05",
                "Police report": "true", "Member ID": "M1"}
        base.update(values)
        return base

    def test_each_fix_does_its_one_thing(self):
        cleaned = self.run_clean([self.row(
            **{"First Name": "  ANN   MARIE ", "Email": "Ann@Example.COM",
               "State": "california", "Claim Amount": "$1,250.5", "Visits": "3.0",
               "Date of Loss": "Mar 5, 2024", "Police report": "Yes"})])
        record = cleaned.records[0]
        self.assertEqual(record["first_name"], "Ann Marie")
        self.assertEqual(record["email"], "ann@example.com")
        self.assertEqual(record["state"], "CA")
        self.assertEqual(record["amount"], "1250.50")
        self.assertEqual(record["visits"], "3")
        self.assertEqual(record["loss_date"], "2024-03-05")
        self.assertIs(record["police"], True)
        self.assertEqual(cleaned.problems, [])

    def test_the_records_have_the_forms_shape(self):
        record = self.run_clean([self.row()]).records[0]
        self.assertEqual(list(record), [
            "first_name", "email", "state", "amount", "visits", "loss_date",
            "police", "member_id"])

    def test_blank_words_are_blank_unless_the_form_offers_them(self):
        record = self.run_clean([self.row(**{"First Name": "N/A", "State": "n/a"})]).records[0]
        self.assertEqual(record["first_name"], "")
        self.assertEqual(record["state"], "N/A")

    def test_a_fix_that_is_off_still_counts_what_it_would_do(self):
        cleaned = self.run_clean([self.row(State="california")], fixes=set())
        options = next(f for f in cleaned.fixes if f.key == "options")
        self.assertFalse(options.on)
        self.assertEqual(options.count, 1)
        self.assertEqual(options.examples[0],
                         {"field": "state", "before": "california", "after": "CA"})
        self.assertEqual(cleaned.records[0]["state"], "california")
        self.assertEqual(cleaned.problems[0]["field"], "state")
        self.assertEqual(cleaned.problems[0]["kind"], "not one of its options")

    def test_invalid_values_are_reported_and_only_emptied_on_request(self):
        rows = [self.row(**{"Claim Amount": "lots", "Visits": "2.5",
                            "Date of Loss": "someday"}),
                self.row(**{"Claim Amount": "99999"})]
        kept = self.run_clean(rows)
        kinds = {(p["field"], p["kind"]) for p in kept.problems}
        self.assertEqual(kinds, {("amount", "not a number"),
                                 ("visits", "not a whole number"),
                                 ("loss_date", "not a date in the form's format"),
                                 ("amount", "above the maximum")})
        self.assertEqual(kept.records[0]["amount"], "lots")

        emptied = self.run_clean(rows, realdata.DEFAULT_FIXES | {"invalid"})
        self.assertEqual(emptied.problems, [])
        self.assertEqual(emptied.records[0]["amount"], "")
        self.assertEqual(emptied.records[1]["amount"], "")
        self.assertEqual(next(f for f in emptied.fixes if f.key == "invalid").count, 4)

    def test_rows_dropped_and_counted(self):
        rows = [self.row(), self.row(), self.row(**{"Member ID": ""}),
                {k: "-" for k in self.row()}]
        default = self.run_clean(rows)
        self.assertEqual(default.rows_in, 4)
        self.assertEqual(default.empty_rows, 1)
        self.assertEqual(default.rows_out, 2)  # the duplicate went, the gap stays
        incomplete = next(f for f in default.fixes if f.key == "incomplete")
        self.assertEqual((incomplete.on, incomplete.count), (False, 1))

        strict = self.run_clean(rows, realdata.DEFAULT_FIXES | {"incomplete"})
        self.assertEqual(strict.rows_out, 1)
        # With nothing reading "-" as empty, the dashes are a row like any other.
        loose = self.run_clean(rows, {"trim"})
        self.assertEqual(loose.rows_out, 4)

    def test_the_limit_is_said_out_loud(self):
        rows = [self.row(**{"Member ID": f"M{n}"}) for n in range(7)]
        table = read_table(to_csv(rows), "f.csv")
        cleaned = clean(form(), table, limit=5)
        self.assertEqual((cleaned.rows_out, cleaned.truncated), (5, 2))

    def test_capitals_leave_units_alone(self):
        street = Field(name="s", semantic_type="street_address")
        self.assertEqual(realdata._case(street, "12 O'BRIEN-SMITH LN APT 7B"),
                         "12 O'Brien-Smith Ln Apt 7B")
        self.assertEqual(realdata._case(street, "# 7B"), "# 7B")

    def test_a_yes_no_text_box_keeps_its_words(self):
        f = Field(name="c", semantic_type="boolean", data_type="boolean")
        self.assertEqual(realdata._types(f, "y"), "Yes")
        self.assertEqual(realdata._types(f, "FALSE"), "No")

    def test_unknown_fix_is_refused(self):
        with self.assertRaises(DataError):
            self.run_clean([self.row()], {"polish"})


class TestRoundTrip(unittest.TestCase):
    """Generated records, written out and cleaned back, are unchanged."""

    def test_every_example_in_every_format(self):
        for path in sorted(EXAMPLES.glob("*")):
            if path.suffix not in (".html", ".json"):
                continue
            schema = extract_html(path) if path.suffix == ".html" else extract_spec(path)
            dataset = generate(schema, count=120, seed=11)
            # Generated text has the odd trailing space; trimming it is the
            # cleaner doing its job, not a difference in meaning.
            expected = [{k: v.strip() if isinstance(v, str) else v for k, v in r.items()}
                        for r in dataset.records]
            for fmt in ("csv", "json", "ndjson"):
                table = read_table(dataset.render(fmt), f"x.{fmt}")
                cleaned = clean(schema, table)
                with self.subTest(form=path.name, fmt=fmt):
                    self.assertEqual(cleaned.rows_out, len(expected))
                    self.assertEqual(cleaned.problems, [])
                    for want, got in zip(expected, cleaned.records):
                        self.assertEqual({k: cell(v) for k, v in want.items()},
                                         {k: cell(v) for k, v in got.items()})


class TestSpecFromTable(unittest.TestCase):
    def test_columns_become_fields_of_the_right_shape(self):
        rows = []
        for n in range(40):
            rows.append({
                "Member ID": f"M{1000 + n}", "Plan": ["Gold", "Silver"][n % 2],
                "Start Date": f"03/{n % 28 + 1:02d}/2024", "Dependents": str(n % 4),
                "Premium": f"{100 + n}.50", "Paperless": ["Yes", "No"][n % 2],
                "ZIP": f"0{2100 + n}", "Notes": "x" * (130 if n == 0 else 3),
            })
        spec = realdata.spec_from_table(read_table(to_csv(rows), "f.csv"), "My Form")
        shapes = {f["label"]: f for f in spec["fields"]}
        self.assertEqual(spec["name"], "my_form")
        self.assertEqual(shapes["Member ID"]["name"], "member_id")
        self.assertNotIn("control", shapes["Member ID"])
        self.assertEqual(shapes["Plan"]["options"], ["Gold", "Silver"])
        self.assertEqual(shapes["Start Date"]["control"], "date")
        self.assertEqual(shapes["Dependents"]["data_type"], "integer")
        self.assertEqual(shapes["Premium"]["data_type"], "number")
        self.assertEqual(shapes["Paperless"]["control"], "checkbox")
        self.assertNotIn("control", shapes["ZIP"])  # a leading zero is an identifier
        self.assertEqual(shapes["Notes"]["control"], "textarea")

    def test_the_spec_reads_and_cleans_like_any_other(self):
        schema = extract_spec(EXAMPLES / "member_enrollment.fields.json")
        records = generate(schema, count=150, seed=4).render("csv")
        table = read_table(records, "f.csv")
        spec = realdata.spec_from_table(table, "members")
        inferred = infer(spec_loader.load(spec))
        cleaned = clean(inferred, table)
        self.assertEqual(cleaned.rows_out, 150)
        self.assertEqual(cleaned.missing, [])
        self.assertEqual(cleaned.problems, [])


if __name__ == "__main__":
    unittest.main()
