"""Tests for the FillerAI web API.

The handlers are called through a real HTTP server on an ephemeral port, so
routing, body limits and error mapping are covered rather than just the
functions underneath them.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.store import Store
from fillerai.web import server as server_module
from fillerai.web.server import MAX_BODY_BYTES, Handler, create_server


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.quiet = True
        # Every stage writes what it produced into the library, so the tests
        # get one of their own rather than filling the working directory with
        # a few hundred megabytes of models.
        cls.library_dir = tempfile.mkdtemp(prefix="fillerai-test-library-")
        cls._real_library = server_module.LIBRARY
        server_module.LIBRARY = Store(cls.library_dir)
        cls.httpd = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server_module.LIBRARY = cls._real_library
        shutil.rmtree(cls.library_dir, ignore_errors=True)

    # -- helpers --------------------------------------------------------

    def post(self, path: str, payload=None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            self.base + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def example_schema(self):
        status, body = self.post("/api/example", {"id": "claims_intake.html"})
        self.assertEqual(status, 200)
        status, body = self.post("/api/extract", {"kind": "html", "content": body["content"]})
        self.assertEqual(status, 200)
        return body["schema"]


class TestStatic(ServerCase):
    def test_index_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"FillerAI", body)

    def test_assets_are_served_with_sensible_types(self):
        for path, expected in (("/static/app.js", "javascript"), ("/static/styles.css", "css")):
            status, _, headers = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers["Content-Type"])

    def test_traversal_out_of_the_static_directory_is_refused(self):
        for path in ("/static/../server.py", "/static/../../schema.py",
                     "/static/../__init__.py"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_unknown_path_is_a_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_responses_carry_hardening_headers(self):
        _, _, headers = self.get("/")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


class TestMeta(ServerCase):
    def test_meta_describes_the_ui(self):
        status, body, _ = self.get("/api/meta")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["version"], fillerai.__version__)
        self.assertIn("postal_code", payload["semantic_types"])
        self.assertTrue(any(e["id"] == "claims_intake.html" for e in payload["examples"]))

    def test_missing_examples_directory_is_not_an_error(self):
        # Installed away from the repository there is no examples/ directory;
        # the UI must still start rather than fail on an empty list.
        from fillerai.web import server

        original = server.EXAMPLES_DIR
        server.EXAMPLES_DIR = Path("/nonexistent-examples-dir")
        try:
            self.assertEqual(server._list_examples(), [])
            self.assertEqual(server.api_meta({})["examples"], [])
        finally:
            server.EXAMPLES_DIR = original

    def test_examples_are_named_not_pathed(self):
        _, body, _ = self.get("/api/meta")
        for entry in json.loads(body)["examples"]:
            self.assertNotIn("/", entry["id"])


class TestExample(ServerCase):
    def test_bundled_example_is_readable(self):
        status, body = self.post("/api/example", {"id": "patient_registration.fields.json"})
        self.assertEqual(status, 200)
        self.assertIn("patient_first_name", body["content"])

    def test_arbitrary_paths_are_refused(self):
        for bad in ("../../../etc/passwd", "/etc/passwd", "server.py", "../README.md"):
            status, body = self.post("/api/example", {"id": bad})
            self.assertEqual(status, 404, bad)
            self.assertIn("error", body)


class TestExtract(ServerCase):
    def test_html_becomes_a_schema(self):
        status, body = self.post("/api/extract", {
            "kind": "html",
            "content": '<label for="e">Email</label><input id="e" name="email" type="email" required>',
        })
        self.assertEqual(status, 200)
        field = body["schema"]["fields"][0]
        self.assertEqual(field["semantic_type"], "email")
        self.assertEqual(body["summary"]["fields"], 1)

    def test_field_spec_becomes_a_schema(self):
        spec = json.dumps({"name": "t", "fields": [{"name": "home_city", "label": "City"}]})
        status, body = self.post("/api/extract", {"kind": "spec", "content": spec})
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"]["fields"][0]["semantic_type"], "city")

    def test_a_saved_schema_round_trips(self):
        schema = self.example_schema()
        status, body = self.post("/api/extract",
                                 {"kind": "spec", "content": json.dumps(schema)})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["schema"]["fields"]), len(schema["fields"]))

    def test_the_bundled_example_is_fully_understood(self):
        status, body = self.post("/api/example", {"id": "claims_intake.html"})
        status, body = self.post("/api/extract", {"kind": "html", "content": body["content"]})
        self.assertEqual(body["summary"]["fields"], 45)
        self.assertEqual(body["summary"]["screens"], 4)
        self.assertEqual(body["summary"]["needs_review"], [])

    def test_useless_input_gets_a_readable_message(self):
        for payload, fragment in (
            ({"kind": "html", "content": "   "}, "nothing to extract"),
            ({"kind": "html", "content": "<p>no form here</p>"}, "no form fields"),
            ({"kind": "spec", "content": "{not json"}, "not valid JSON"),
            ({"kind": "spec", "content": '{"name":"x"}'}, "fields"),
            ({"kind": "elsewhere", "content": "x"}, "unknown source kind"),
            ({"kind": "html"}, "missing 'content'"),
        ):
            status, body = self.post("/api/extract", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn(fragment, body["error"], payload)


class TestGenerate(ServerCase):
    def test_records_come_back_checked(self):
        schema = self.example_schema()
        status, body = self.post("/api/generate", {"schema": schema, "count": 25, "seed": 42})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 25)
        self.assertEqual(body["problems"], [])
        self.assertNotIn("claim_number", body["columns"], "read-only field was offered")

    def test_the_same_seed_gives_the_same_records(self):
        schema = self.example_schema()
        first = self.post("/api/generate", {"schema": schema, "count": 5, "seed": 7})[1]
        second = self.post("/api/generate", {"schema": schema, "count": 5, "seed": 7})[1]
        self.assertEqual(first["records"], second["records"])

    def test_an_edited_semantic_type_is_obeyed(self):
        # This is what the schema editor does: set the meaning and mark it
        # authoritative. Generation must follow it, not re-infer around it.
        schema = self.example_schema()
        for field in schema["fields"]:
            if field["name"] == "policy_number":
                field["semantic_type"] = "email"
                field["confidence"] = 1.0
        status, body = self.post("/api/generate", {"schema": schema, "count": 3, "seed": 1})
        self.assertEqual(status, 200)
        for record in body["records"]:
            self.assertIn("@", record["policy_number"])

    def test_bad_parameters_are_explained(self):
        schema = self.example_schema()
        for payload, fragment in (
            ({"schema": schema, "count": 0}, "between 1 and"),
            ({"schema": schema, "count": 99999}, "between 1 and"),
            ({"schema": schema, "seed": "soon"}, "whole number"),
            ({"schema": schema, "blank_rate": 4}, "between 0 and 1"),
            ({"schema": schema, "blank_rate": "lots"}, "must be a number"),
            ({}, "missing 'schema'"),
            ({"schema": {"schema_version": "9.0", "name": "x", "fields": []}}, "not compatible"),
        ):
            status, body = self.post("/api/generate", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn(fragment, body["error"], payload)


class TestExport(ServerCase):
    def setUp(self):
        self.schema = self.example_schema()
        self.records = self.post(
            "/api/generate", {"schema": self.schema, "count": 4, "seed": 3}
        )[1]["records"]

    def test_each_format_exports(self):
        for fmt, check in (
            ("csv", lambda t: len(t.strip().splitlines()) == 5),
            ("json", lambda t: len(json.loads(t)["records"]) == 4),
            ("ndjson", lambda t: len(t.strip().splitlines()) == 4),
        ):
            status, body = self.post("/api/export", {
                "schema": self.schema, "records": self.records, "format": fmt,
            })
            self.assertEqual(status, 200, fmt)
            self.assertTrue(check(body["text"]), fmt)
            self.assertTrue(body["filename"].endswith(fmt), fmt)

    def test_unknown_format_is_refused(self):
        status, body = self.post("/api/export", {
            "schema": self.schema, "records": self.records, "format": "xlsx",
        })
        self.assertEqual(status, 400)
        self.assertIn("unknown output format", body["error"])

    def test_records_must_be_a_list(self):
        status, body = self.post("/api/export", {"schema": self.schema, "records": {}})
        self.assertEqual(status, 400)
        self.assertIn("must be a list", body["error"])


class TestRequestHandling(ServerCase):
    def test_malformed_body_does_not_crash_the_server(self):
        status, body = self.post("/api/extract", raw=b"this is not json")
        self.assertEqual(status, 400)
        self.assertIn("not valid JSON", body["error"])
        # The server is still answering afterwards.
        self.assertEqual(self.get("/api/meta")[0], 200)

    def test_non_object_body_is_refused(self):
        status, body = self.post("/api/extract", raw=b"[1, 2, 3]")
        self.assertEqual(status, 400)
        self.assertIn("must be a JSON object", body["error"])

    def test_oversized_body_is_refused_before_reading(self):
        oversized = b'{"content": "' + b"x" * (MAX_BODY_BYTES + 10) + b'"}'
        status, body = self.post("/api/extract", raw=oversized)
        self.assertEqual(status, 413)
        self.assertIn("too large", body["error"])

    def test_unknown_endpoint_is_a_404(self):
        status, body = self.post("/api/imaginary", {})
        self.assertEqual(status, 404)
        self.assertIn("no endpoint", body["error"])


class TestTrainApi(ServerCase):
    """The Train panel's endpoints, over the wire the browser uses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = fillerai.extract_html(
            Path(__file__).resolve().parent.parent / "examples" / "claims_intake.html")
        cls.records = fillerai.generate(cls.schema, count=250, seed=42).records

    def trained(self):
        status, body = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records, "seed": 1,
        })
        self.assertEqual(status, 200)
        return body

    def test_training_reports_what_it_can_and_cannot_fill(self):
        body = self.trained()
        self.assertEqual(body["counts"]["predictable"] + body["counts"]["yours"],
                         body["counts"]["total"])
        self.assertEqual(len(body["fields"]), body["counts"]["total"])
        self.assertTrue(body["seeds"])
        self.assertGreater(body["trained_on"], 0)

    def test_a_small_run_measures_no_vote_weights_and_says_so(self):
        # Under the holdout the combiner needs, nothing is taken for it and
        # nothing was measured. The key is still there - the browser decides
        # from the vote count whether there is a panel to draw at all.
        body = self.trained()
        self.assertIn("combiner", body)
        self.assertEqual(body["combiner"]["votes"], 0)
        self.assertFalse(body["combiner"]["fitted"])

    def test_a_run_large_enough_carries_the_fitted_vote_weights(self):
        records = fillerai.generate(self.schema, count=1200, seed=42).records
        status, body = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": records, "seed": 1,
        })
        self.assertEqual(status, 200)
        combiner = body["combiner"]
        # One weight per feature, and the two numbers that decided whether the
        # fitted weights were kept. Everything the panel draws, in other words.
        self.assertGreater(combiner["votes"], 0)
        self.assertEqual(len(combiner["weights"]), len(combiner["features"]))
        self.assertIn("heuristic", combiner["features"])
        self.assertGreater(combiner["baseline_loss"], 0)
        self.assertGreater(combiner["loss"], 0)

    def test_the_score_is_measured_on_records_held_back_from_the_fit(self):
        body = self.trained()
        self.assertGreater(body["held_out"], 0)
        self.assertEqual(body["trained_on"] + body["held_out"], len(self.records))
        self.assertIn("headline", body["evaluation"])
        self.assertGreater(body["evaluation"]["accepted_accuracy"], 0.8)

    def test_predicting_fills_the_rest_of_the_form(self):
        body = self.trained()
        seed = body["seeds"][0]
        value = next(r[seed] for r in self.records if r[seed])
        status, result = self.post("/api/predict", {
            "model_id": body["model_id"], "observed": {seed: value},
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["given"], {seed: value})
        names = {p["field"] for p in result["predictions"]}
        self.assertNotIn(seed, names)  # the caller typed it; it is not a guess
        self.assertGreater(result["offered"], 0)

    def test_every_prediction_carries_a_confidence_and_a_reason(self):
        body = self.trained()
        status, result = self.post("/api/predict", {"model_id": body["model_id"]})
        self.assertEqual(status, 200)
        for prediction in result["predictions"]:
            self.assertIn(prediction["basis"], ("rule", "learned", "usual", "none"))
            self.assertGreaterEqual(prediction["confidence"], 0.0)
            self.assertLessEqual(prediction["confidence"], 1.0)
            if prediction["value"] is None:
                self.assertTrue(prediction.get("because"))

    def test_a_higher_threshold_offers_fewer_answers(self):
        body = self.trained()
        seed = body["seeds"][0]
        value = next(r[seed] for r in self.records if r[seed])
        counts = []
        for threshold in (0.2, 0.9):
            _, result = self.post("/api/predict", {
                "model_id": body["model_id"], "observed": {seed: value},
                "threshold": threshold,
            })
            counts.append(result["offered"])
        self.assertGreaterEqual(counts[0], counts[1])

    def test_the_model_can_be_downloaded_as_the_file_the_cli_reads(self):
        from fillerai.train.model import AutofillModel

        body = self.trained()
        status, result = self.post("/api/model", {"model_id": body["model_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["filename"].endswith(".model.json"))
        model = AutofillModel.from_json(result["text"])
        self.assertEqual(model.targets(), [f["name"] for f in body["fields"]])

    def test_a_model_the_server_no_longer_holds_says_what_to_do(self):
        # The cache is small and the server may have restarted. The UI has to
        # be able to tell this apart from a real failure.
        status, body = self.post("/api/predict", {"model_id": "gone", "observed": {}})
        self.assertEqual(status, 404)
        self.assertIn("train it again", body["error"])

    def test_the_cache_holds_a_bounded_number_of_models(self):
        from fillerai.web.server import MAX_CACHED_MODELS

        tokens = [self.trained()["model_id"] for _ in range(MAX_CACHED_MODELS + 1)]
        self.assertEqual(self.post("/api/predict",
                                   {"model_id": tokens[-1], "observed": {}})[0], 200)
        # The oldest was evicted rather than the process growing without end.
        self.assertEqual(self.post("/api/predict",
                                   {"model_id": tokens[0], "observed": {}})[0], 404)

    def test_training_without_records_says_so_plainly(self):
        status, body = self.post("/api/train",
                                 {"schema": self.schema.to_dict(), "records": []})
        self.assertEqual(status, 400)
        self.assertIn("generate some first", body["error"])

    def test_records_that_are_not_objects_are_refused(self):
        status, body = self.post("/api/train",
                                 {"schema": self.schema.to_dict(), "records": [1, 2]})
        self.assertEqual(status, 400)
        self.assertIn("field names to values", body["error"])

    def test_a_field_the_model_never_saw_is_named_in_the_error(self):
        body = self.trained()
        status, result = self.post("/api/predict", {
            "model_id": body["model_id"], "observed": {"not_a_field": "x"},
        })
        self.assertEqual(status, 400)
        self.assertIn("not_a_field", result["error"])

    def test_a_bad_seed_or_threshold_is_explained_not_crashed(self):
        status, body = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records, "seed": "soon",
        })
        self.assertEqual(status, 400)
        self.assertIn("whole number", body["error"])

        trained = self.trained()
        status, body = self.post("/api/predict", {
            "model_id": trained["model_id"], "observed": {}, "threshold": 5,
        })
        self.assertEqual(status, 400)
        self.assertIn("between 0 and 1", body["error"])


class TestSimulateApi(ServerCase):
    """The Simulate panel's endpoints, over the wire the browser uses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = fillerai.extract_html(
            Path(__file__).resolve().parent.parent / "examples" / "claims_intake.html")
        cls.records = fillerai.generate(cls.schema, count=250, seed=42).records

    def model_id(self):
        status, body = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records, "seed": 1,
        })
        self.assertEqual(status, 200)
        return body["model_id"]

    def test_the_form_comes_back_ready_to_draw(self):
        status, body = self.post("/api/simulate/form", {"model_id": self.model_id()})
        self.assertEqual(status, 200)
        layout = body["layout"]
        fillable = [f.name for f in self.schema.fields if not f.constraints.read_only]
        names = [c["name"] for page in layout["pages"]
                 for section in page["sections"] for c in section["controls"]]
        self.assertEqual(names, fillable)
        self.assertEqual([p["id"] for p in layout["pages"]],
                         [s.id for s in self.schema.screens])
        # The seeds and the cost assumptions travel with the form, so the
        # panel never has to invent either of them.
        self.assertTrue(body["seeds"])
        self.assertTrue(body["assumptions"])

    def test_the_form_says_which_model_is_filling_it_in(self):
        status, body = self.post("/api/simulate/form", {"model_id": self.model_id()})
        self.assertEqual(status, 200)
        about = body["model"]
        # What the model is, without having to ask another endpoint for it.
        self.assertEqual(about["algorithm"], "statistical")
        self.assertTrue(about["algorithm_label"])
        self.assertEqual(about["form"], self.schema.name)
        self.assertGreater(about["trained_on"], 0)
        self.assertGreater(about["fields"], 0)
        # And the chain it came out of, oldest first, ending in the model
        # itself. The schema was posted rather than extracted here, so there
        # is no page above it.
        self.assertEqual([e["kind"] for e in about["lineage"]],
                         ["schema", "dataset", "model"])
        self.assertEqual(about["entry"]["id"], about["lineage"][-1]["id"])

    def test_a_model_reopened_from_the_library_still_knows_its_chain(self):
        status, trained = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records, "seed": 1,
        })
        self.assertEqual(status, 200)
        entry_id = trained["model_entry_id"]
        # A fresh handle from the library, as opening a model in the browser
        # gives: it has to be as traceable as the one training just produced.
        _, opened = self.post("/api/library/open", {"id": entry_id})
        _, body = self.post("/api/simulate/form", {"model_id": opened["model_id"]})
        self.assertEqual(body["model"]["entry"]["id"], entry_id)
        self.assertEqual([e["kind"] for e in body["model"]["lineage"]],
                         ["schema", "dataset", "model"])

    def test_a_model_that_was_never_saved_says_so_rather_than_inventing_a_chain(self):
        status, trained = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records,
            "seed": 1, "save": False,
        })
        self.assertEqual(status, 200)
        _, body = self.post("/api/simulate/form", {"model_id": trained["model_id"]})
        self.assertIsNone(body["model"]["entry"])
        self.assertEqual(body["model"]["lineage"], [])
        # It is still a model, and can still say what it is.
        self.assertEqual(body["model"]["algorithm"], "statistical")

    def test_a_model_whose_entry_was_deleted_reports_no_chain(self):
        status, trained = self.post("/api/train", {
            "schema": self.schema.to_dict(), "records": self.records, "seed": 1,
        })
        self.assertEqual(status, 200)
        status, _ = self.post("/api/library/delete",
                              {"id": trained["model_entry_id"], "cascade": True})
        self.assertEqual(status, 200)
        # The model is still loaded and still fills forms; what is gone is the
        # record of where it came from, and a half-drawn chain would be worse
        # than none.
        _, body = self.post("/api/simulate/form", {"model_id": trained["model_id"]})
        self.assertIsNone(body["model"]["entry"])
        self.assertEqual(body["model"]["lineage"], [])

    def test_a_case_is_a_record_the_model_was_not_trained_on(self):
        status, body = self.post("/api/simulate/case",
                                 {"model_id": self.model_id(), "seed": 4242})
        self.assertEqual(status, 200)
        self.assertEqual(body["seed"], 4242)
        self.assertNotIn(body["case"], self.records)
        # Pinning the seed gets the same form back, which is what makes a
        # run worth looking at twice.
        _, again = self.post("/api/simulate/case",
                             {"model_id": self.model_id(), "seed": 4242})
        self.assertEqual(again["case"], body["case"])

    def test_a_random_case_reports_the_seed_it_used(self):
        status, body = self.post("/api/simulate/case", {"model_id": self.model_id()})
        self.assertEqual(status, 200)
        self.assertIsInstance(body["seed"], int)

    def test_filling_reports_the_board_the_saving_and_the_score(self):
        model_id = self.model_id()
        _, form = self.post("/api/simulate/form", {"model_id": model_id})
        _, case = self.post("/api/simulate/case", {"model_id": model_id, "seed": 7})
        typed = {name: case["case"][name] for name in form["seeds"]
                 if str(case["case"].get(name) or "").strip()}

        status, body = self.post("/api/simulate/fill", {
            "model_id": model_id, "typed": typed, "case": case["case"],
        })
        self.assertEqual(status, 200)
        sources = {cell["source"] for cell in body["cells"]}
        self.assertTrue(sources <= {"typed", "filled", "suggested", "yours"})
        self.assertEqual(body["savings"]["typed"], len(typed))
        self.assertEqual(
            body["savings"]["typed"] + body["savings"]["filled"]
            + body["savings"]["left"], body["savings"]["fields"])
        self.assertIsNotNone(body["score"])
        # The headline always compares against filling the form by hand;
        # the saving on its own would not say what it is a saving against.
        self.assertIn("by hand", body["headline"])

    def test_without_a_case_there_is_a_board_but_nothing_to_score(self):
        status, body = self.post("/api/simulate/fill", {"model_id": self.model_id()})
        self.assertEqual(status, 200)
        self.assertIsNone(body["score"])
        self.assertTrue(body["cells"])

    def test_raising_the_bar_fills_less_of_the_form(self):
        model_id = self.model_id()
        _, form = self.post("/api/simulate/form", {"model_id": model_id})
        _, case = self.post("/api/simulate/case", {"model_id": model_id, "seed": 7})
        typed = {name: case["case"][name] for name in form["seeds"]
                 if str(case["case"].get(name) or "").strip()}
        low = self.post("/api/simulate/fill", {
            "model_id": model_id, "typed": typed, "threshold": 0.3})[1]
        high = self.post("/api/simulate/fill", {
            "model_id": model_id, "typed": typed, "threshold": 0.99})[1]
        self.assertGreaterEqual(low["savings"]["filled"], high["savings"]["filled"])

    def test_a_sweep_averages_over_forms_the_model_has_not_seen(self):
        model_id = self.model_id()
        _, form = self.post("/api/simulate/form", {"model_id": model_id})
        status, body = self.post("/api/simulate/sweep", {
            "model_id": model_id, "seeds": form["seeds"], "count": 12, "seed": 5,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["cases"], 12)
        self.assertEqual(body["seeds"], form["seeds"])
        self.assertIn("per_case", body)
        self.assertTrue(body["assumptions"])

    def test_the_sweep_is_bounded_so_one_request_cannot_run_away(self):
        status, body = self.post("/api/simulate/sweep",
                                 {"model_id": self.model_id(), "count": 100000})
        self.assertEqual(status, 400)
        self.assertIn("between 1 and", body["error"])

    def test_a_model_that_has_fallen_out_of_the_cache_says_what_to_do(self):
        for path in ("/api/simulate/form", "/api/simulate/case",
                     "/api/simulate/fill", "/api/simulate/sweep"):
            status, body = self.post(path, {"model_id": "not-a-model"})
            self.assertEqual(status, 404, path)
            self.assertIn("train it again", body["error"])

    def test_the_errors_the_panel_can_show_are_sentences(self):
        model_id = self.model_id()
        status, body = self.post("/api/simulate/fill",
                                 {"model_id": model_id, "typed": {"nope": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("nope", body["error"])

        status, body = self.post("/api/simulate/fill",
                                 {"model_id": model_id, "threshold": 7})
        self.assertEqual(status, 400)
        self.assertIn("between 0 and 1", body["error"])

        status, body = self.post("/api/simulate/fill",
                                 {"model_id": model_id, "typed": "not an object"})
        self.assertEqual(status, 400)
        self.assertIn("object", body["error"])

        status, body = self.post("/api/simulate/fill",
                                 {"model_id": model_id, "case": [1, 2]})
        self.assertEqual(status, 400)
        self.assertIn("object", body["error"])


class TestAlgorithmChoice(ServerCase):
    """Choosing how the model learns, over the wire."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = fillerai.extract_html(
            Path(__file__).resolve().parent.parent / "examples" / "claims_intake.html")
        cls.records = fillerai.generate(cls.schema, count=120, seed=42).records

    def request(self, **extra):
        body = {"schema": self.schema.to_dict(), "records": self.records,
                "seed": 1, "save": False}
        body.update(extra)
        return body

    def test_the_meta_call_offers_every_algorithm_with_its_recipe(self):
        status, body = self.post("/api/meta", {})
        self.assertEqual(status, 200)
        names = [a["name"] for a in body["algorithms"]]
        self.assertIn("statistical", names)
        self.assertIn("tree", names)
        self.assertIn(body["default_algorithm"], names)
        for algorithm in body["algorithms"]:
            self.assertTrue(algorithm["recipe"])
            self.assertTrue(algorithm["blurb"])

    def test_each_algorithm_trains_and_says_which_one_it_was(self):
        for name in fillerai.algos.names():
            with self.subTest(algorithm=name):
                status, body = self.post("/api/train", self.request(algorithm=name))
                self.assertEqual(status, 200)
                self.assertEqual(body["algorithm"], name)
                self.assertTrue(body["engine"])

    def test_an_algorithm_that_does_not_exist_is_refused_by_name(self):
        status, body = self.post("/api/train", self.request(algorithm="telepathy"))
        self.assertEqual(status, 400)
        self.assertIn("telepathy", body["error"])

    def test_a_setting_outside_its_declared_range_is_refused(self):
        """The browser sends whatever is on screen; a depth of 900 is a hang."""
        status, body = self.post(
            "/api/train", self.request(algorithm="forest", tuning={"max_depth": 900}))
        self.assertEqual(status, 400)
        self.assertIn("max_depth", body["error"])

    def test_a_setting_for_another_algorithm_is_ignored_rather_than_refused(self):
        status, body = self.post(
            "/api/train", self.request(algorithm="tree", tuning={"neighbours": 9}))
        self.assertEqual(status, 200)
        self.assertNotIn("neighbours", body["settings"]["tuning"])

    def test_the_tree_can_be_drawn_for_a_field(self):
        status, body = self.post("/api/train", self.request(algorithm="tree"))
        self.assertEqual(status, 200)
        self.assertTrue(body["drawable"])
        status, drawn = self.post("/api/train/tree", {
            "model_id": body["model_id"], "field": body["drawable"][0]})
        self.assertEqual(status, 200)
        self.assertTrue(drawn["lines"])

    def test_an_engine_with_no_tree_says_so_rather_than_failing(self):
        status, body = self.post("/api/train", self.request(algorithm="statistical"))
        self.assertEqual(body["drawable"], [])
        status, drawn = self.post("/api/train/tree", {
            "model_id": body["model_id"], "field": "home_city"})
        self.assertEqual(status, 200)
        self.assertEqual(drawn["lines"], [])

    def test_turning_the_rules_off_is_carried_through(self):
        status, body = self.post("/api/train", self.request(use_rules=False))
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"], [])
        self.assertFalse(body["settings"]["use_rules"])


class TestWatchedTraining(ServerCase):
    """Starting a run and reading its log while it happens."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = fillerai.extract_html(
            Path(__file__).resolve().parent.parent / "examples" / "claims_intake.html")
        cls.records = fillerai.generate(cls.schema, count=120, seed=42).records

    def run_one(self, **extra):
        body = {"schema": self.schema.to_dict(), "records": self.records,
                "seed": 1, "save": False}
        body.update(extra)
        status, started = self.post("/api/train/start", body)
        self.assertEqual(status, 200)

        cursor, lines, result = 0, [], None
        for _ in range(600):  # a hard bound, so a hung fit fails the test
            status, log = self.post("/api/train/log",
                                    {"run_id": started["run_id"], "cursor": cursor})
            self.assertEqual(status, 200)
            self.assertEqual(log["cursor"], cursor + len(log["lines"]))
            cursor = log["cursor"]
            lines.extend(log["lines"])
            if log["result"] or log["error"]:
                result = log
                break
            time.sleep(0.05)
        self.assertIsNotNone(result, "the run never finished")
        return started, lines, result

    def test_a_run_hands_back_its_script_before_it_has_finished(self):
        status, started = self.post("/api/train/start", {
            "schema": self.schema.to_dict(), "records": self.records,
            "algorithm": "tree", "seed": 1, "save": False})
        self.assertEqual(status, 200)
        self.assertIn("TrainOptions", started["script"])
        self.assertIn("'tree'", started["script"])
        self.assertIn("--algorithm tree", started["command"])
        self.assertTrue(started["recipe"])

    def test_the_log_arrives_in_order_with_nothing_dropped_or_repeated(self):
        _started, lines, result = self.run_one()
        self.assertIsNone(result["error"])
        self.assertEqual([line["index"] for line in lines],
                         list(range(len(lines))))
        self.assertTrue(any(line["level"] == "step" for line in lines))
        self.assertTrue(result["progress"]["finished"])

    def test_the_run_ends_with_the_same_result_the_direct_call_gives(self):
        _started, _lines, result = self.run_one(algorithm="tree")
        self.assertEqual(result["result"]["algorithm"], "tree")
        self.assertTrue(result["result"]["seeds"])
        self.assertGreater(result["result"]["trained_on"], 0)

    def test_a_run_that_fails_reports_it_rather_than_hanging(self):
        status, started = self.post("/api/train/start", {
            "schema": self.schema.to_dict(),
            # Records whose every field is unknown to the schema: the fit has
            # nothing to learn from and raises inside the worker thread.
            "records": [{}], "save": False})
        self.assertEqual(status, 200)
        for _ in range(200):
            status, log = self.post("/api/train/log", {"run_id": started["run_id"]})
            if log["error"] or log["result"] or log["progress"]["finished"]:
                break
            time.sleep(0.05)
        self.assertTrue(log["progress"]["finished"] or log["error"])

    def test_a_run_that_is_no_longer_here_says_so(self):
        status, body = self.post("/api/train/log", {"run_id": "not-a-run"})
        self.assertEqual(status, 404)
        self.assertIn("start it again", body["error"])

    def test_the_script_can_be_asked_for_without_running_anything(self):
        status, body = self.post("/api/train/script",
                                 {"algorithm": "forest", "tuning": {"trees": 7},
                                  "seed": 3})
        self.assertEqual(status, 200)
        self.assertIn("'trees': 7", body["script"])
        self.assertIn("seed=3", body["script"])
        # It has to be a script, not a sketch of one.
        compile(body["script"], "generated.py", "exec")


class TestLibraryApi(ServerCase):
    """What the stages kept, and being able to pick it up again."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = (Path(__file__).resolve().parent.parent
                      / "examples" / "claims_intake.html").read_text(encoding="utf-8")

    def full_chain(self):
        _status, extracted = self.post(
            "/api/extract", {"kind": "html", "content": self.source, "name": "claims"})
        _status, generated = self.post("/api/generate", {
            "schema": extracted["schema"], "count": 80, "seed": 4,
            "schema_id": extracted["schema_id"]})
        _status, trained = self.post("/api/train", {
            "schema": extracted["schema"], "records": generated["records"],
            "algorithm": "tree", "seed": 1,
            "dataset_id": generated["dataset_id"]})
        return extracted, generated, trained

    def test_each_stage_keeps_what_it_produced(self):
        extracted, generated, trained = self.full_chain()
        self.assertTrue(extracted["source_id"].startswith("src-"))
        self.assertTrue(extracted["schema_id"].startswith("sch-"))
        self.assertTrue(generated["dataset_id"].startswith("dat-"))
        self.assertTrue(trained["model_entry_id"].startswith("mdl-"))

    def test_a_model_knows_the_whole_chain_it_came_from(self):
        extracted, generated, trained = self.full_chain()
        status, body = self.post("/api/library", {})
        self.assertEqual(status, 200)
        row = next(e for e in body["entries"] if e["id"] == trained["model_entry_id"])
        self.assertEqual(row["lineage"],
                         [extracted["source_id"], extracted["schema_id"],
                          generated["dataset_id"]])

    def test_opening_a_model_restores_the_stages_behind_it(self):
        extracted, generated, trained = self.full_chain()
        status, body = self.post("/api/library/open", {"id": trained["model_entry_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["algorithm"], "tree")
        self.assertEqual(body["schema_id"], extracted["schema_id"])
        self.assertEqual(body["dataset_id"], generated["dataset_id"])
        self.assertIn("source", body)
        self.assertTrue(body["fields"])
        # Including how loudly each voter was told to speak, so a reopened
        # model describes itself the same way a freshly trained one does.
        self.assertIn("combiner", body)
        # The reopened model is usable immediately, not just describable.
        status, predicted = self.post(
            "/api/predict", {"model_id": body["model_id"], "observed": {}})
        self.assertEqual(status, 200)
        self.assertTrue(predicted["predictions"])

    def test_opening_a_dataset_gives_its_records_back(self):
        _extracted, generated, _trained = self.full_chain()
        status, body = self.post("/api/library/open", {"id": generated["dataset_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["records"]), 80)

    def test_the_listing_can_be_narrowed_to_one_kind(self):
        self.full_chain()
        status, body = self.post("/api/library", {"kind": "model"})
        self.assertEqual(status, 200)
        self.assertTrue(body["entries"])
        self.assertTrue(all(e["kind"] == "model" for e in body["entries"]))

    def test_a_kind_the_library_does_not_hold_is_refused(self):
        status, body = self.post("/api/library", {"kind": "opinion"})
        self.assertEqual(status, 400)

    def test_an_id_that_is_really_a_path_is_refused(self):
        for attempt in ("../../etc/passwd", "mdl-../x", ""):
            with self.subTest(attempt):
                status, _body = self.post("/api/library/open", {"id": attempt})
                self.assertIn(status, (400, 404))

    def test_an_entry_can_be_exported_renamed_and_deleted(self):
        _extracted, _generated, trained = self.full_chain()
        model_id = trained["model_entry_id"]

        status, body = self.post("/api/library/export", {"id": model_id})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body["text"])["algorithm"], "tree")

        status, body = self.post("/api/library/rename",
                                 {"id": model_id, "name": "the good one"})
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["name"], "the good one")

        status, body = self.post("/api/library/delete", {"id": model_id})
        self.assertEqual(status, 200)
        # The script the run wrote goes with the model it trained: it is not
        # something made from the model, it is a description of the run.
        self.assertIn(model_id, body["removed"])
        self.assertIn(trained["script_entry_id"], body["removed"])

        status, _body = self.post("/api/library/open", {"id": model_id})
        self.assertEqual(status, 404)
        status, _body = self.post("/api/library/open",
                                  {"id": trained["script_entry_id"]})
        self.assertEqual(status, 404)

    def test_two_algorithms_on_one_dataset_both_hang_off_it(self):
        """The comparison the library is for."""
        extracted, generated, first = self.full_chain()
        _status, second = self.post("/api/train", {
            "schema": extracted["schema"], "records": generated["records"],
            "algorithm": "nearest", "seed": 1,
            "dataset_id": generated["dataset_id"]})
        status, body = self.post("/api/library", {"under": generated["dataset_id"]})
        self.assertEqual(status, 200)
        ids = {e["id"] for e in body["entries"]}
        self.assertIn(first["model_entry_id"], ids)
        self.assertIn(second["model_entry_id"], ids)
        self.assertEqual({e["meta"]["algorithm"] for e in body["entries"]},
                         {"tree", "nearest"})


if __name__ == "__main__":
    unittest.main()
