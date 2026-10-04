"""The real-records endpoints: upload, clean, save, train on it, test on it."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai import FormSchema, generate
from fillerai.web import server as server_module

from test_web import ServerCase


class RealDataCase(ServerCase):
    def setUp(self):
        self.schema = self.example_schema()
        records = generate(FormSchema.from_dict(self.schema), count=160, seed=8).records
        # What a real export looks like: shouted, padded, with its own
        # spelling of a blank, one row twice over.
        self.rows = [dict(r) for r in records] + [dict(records[0])]
        for row in self.rows[:20]:
            row["home_city"] = f"  {str(row.get('home_city') or '').upper()} "
        from fillerai.generate.dataset import Dataset
        self.text = Dataset(schema=FormSchema.from_dict(self.schema),
                            records=self.rows).to_csv()

    def clean(self, **extra):
        payload = {"schema": self.schema, "text": self.text, "filename": "export.csv"}
        payload.update(extra)
        return self.post("/api/data/clean", payload)


class TestReadAndClean(RealDataCase):
    def test_read_suggests_a_mapping_and_the_fixes(self):
        status, body = self.post("/api/data/read", {
            "schema": self.schema, "text": self.text, "filename": "export.csv"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["format"], "csv")
        self.assertEqual(body["rows"], 161)
        self.assertEqual(body["mapping"]["home_city"], "home_city")
        self.assertEqual(body["mapping"]["claim_number"], "")  # read-only
        self.assertIn("trim", [f["key"] for f in body["fixes"] if f["on"]])
        self.assertNotIn("incomplete", [f["key"] for f in body["fixes"] if f["on"]])
        self.assertLessEqual(len(body["preview"]), 20)

    def test_preview_saves_nothing(self):
        before = server_module.library().totals().get("dataset", 0)
        status, body = self.clean()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], 160)
        self.assertNotIn("dataset_id", body)
        self.assertEqual(server_module.library().totals().get("dataset", 0), before)
        fixes = {f["key"]: f for f in body["report"]["fixes"]}
        self.assertEqual(fixes["duplicates"]["count"], 1)
        self.assertGreaterEqual(fixes["case"]["count"], 1)

    def test_save_keeps_a_real_dataset_under_the_schema(self):
        status, body = self.clean(save=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["records"]), 160)
        library = server_module.library()
        entry = library.get(body["dataset_id"])
        self.assertEqual(entry.meta["origin"], "real")
        self.assertEqual(entry.meta["file"], "export.csv")
        self.assertIn("real records", entry.name)
        self.assertEqual(entry.parent, body["schema_id"])
        self.assertTrue(all(r["home_city"] == r["home_city"].strip()
                            for r in library.load_records(body["dataset_id"])))

    def test_mapping_and_fixes_are_the_callers(self):
        status, body = self.clean(fixes=["trim"], mapping={"home_city": ""})
        self.assertEqual(status, 200, body)
        self.assertIn("home_city", body["report"]["missing"])
        self.assertEqual(body["count"], 161)  # duplicates kept

    def test_bad_requests_say_why(self):
        for payload, words in (
            ({"text": "", "filename": "f.csv"}, "empty"),
            ({"text": self.text, "mapping": {"home_city": "nope"}}, "does not have"),
            ({"text": self.text, "fixes": ["polish"]}, "no fix"),
        ):
            status, body = self.post("/api/data/clean", {"schema": self.schema, **payload})
            self.assertEqual(status, 400)
            self.assertIn(words, body["error"])


class TestSchemaFromData(RealDataCase):
    def test_a_form_from_columns_alone(self):
        status, body = self.post("/api/data/schema", {
            "text": self.text, "filename": "export.csv", "name": "claims export"})
        self.assertEqual(status, 200, body)
        names = [f["name"] for f in body["schema"]["fields"]]
        self.assertIn("home_city", names)
        self.assertTrue(all(body["mapping"].values()))
        source = server_module.library().get(body["source_id"])
        self.assertEqual(source.kind, "source")
        status, cleaned = self.post("/api/data/clean", {
            "schema": body["schema"], "schema_id": body["schema_id"],
            "text": self.text, "filename": "export.csv"})
        self.assertEqual(status, 200, cleaned)
        self.assertEqual(cleaned["count"], 160)


class TestTrainAndTest(RealDataCase):
    def train(self, records, dataset_id=None):
        status, body = self.post("/api/train", {
            "schema": self.schema, "records": records, "seed": 3,
            "dataset_id": dataset_id})
        self.assertEqual(status, 200, body)
        return body

    def test_train_on_real_then_lineage_runs_through_it(self):
        _, saved = self.clean(save=True)
        model = self.train(saved["records"], saved["dataset_id"])
        self.assertEqual(model["dataset_id"], saved["dataset_id"])
        lineage = server_module.library().lineage(model["model_entry_id"])
        self.assertIn(saved["dataset_id"], [e.id for e in lineage])

    def test_a_synthetic_model_tested_on_real_records(self):
        synthetic = generate(FormSchema.from_dict(self.schema), count=200, seed=1).records
        model = self.train(synthetic)
        _, saved = self.clean(save=True)
        status, body = self.post("/api/evaluate", {
            "model_id": model["model_id"], "dataset_id": saved["dataset_id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["records"], 160)
        self.assertFalse(body["learned_from"])
        self.assertEqual(body["dataset"]["id"], saved["dataset_id"])
        self.assertIn("filled", body["evaluation"]["headline"])
        self.assertIn("over 160 forms", body["sweep"]["headline"])
        self.assertEqual(body["fields_present"], body["fields_total"])

    def test_testing_on_the_records_it_learned_from_is_flagged(self):
        _, saved = self.clean(save=True)
        model = self.train(saved["records"], saved["dataset_id"])
        status, body = self.post("/api/evaluate", {
            "model_id": model["model_id"], "dataset_id": saved["dataset_id"]})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["learned_from"])

    def test_records_for_another_form_are_refused(self):
        model = self.train(generate(FormSchema.from_dict(self.schema), count=60,
                                    seed=2).records)
        status, body = self.post("/api/evaluate", {
            "model_id": model["model_id"], "records": [{"unrelated": "x"}]})
        self.assertEqual(status, 400)
        self.assertIn("another form", body["error"])
        status, body = self.post("/api/evaluate", {
            "model_id": model["model_id"], "dataset_id": "dat-missing"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
