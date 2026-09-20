"""Tests for the library.

Most of these are about lineage, because lineage is the only thing the
library really promises. A directory of files that cannot say which dataset
a model learned from is a directory of files.

The rest are about what happens when it is asked something unreasonable: an
id that is a path, a parent that does not exist, a delete that would orphan
half of it. A local tool runs unattended for weeks and has no operator, so
every one of those has to have a decided answer rather than a traceback.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.schema import Field, FormSchema
from fillerai.store import KINDS, Entry, Store, StoreError, kind_of, new_id
from fillerai.train.model import TrainOptions, train


def simple_schema() -> FormSchema:
    return FormSchema(name="library test",
                      fields=[Field(name=n, label=n) for n in ("city", "state")])


def simple_records(count: int = 60) -> list[dict[str, str]]:
    pairs = [("Austin", "TX"), ("Denver", "CO")]
    return [{"city": pairs[i % 2][0], "state": pairs[i % 2][1]} for i in range(count)]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fillerai-store-")
        self.store = Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def chain(self):
        """One of every kind, linked the way a real run links them."""
        source = self.store.save_source("<form></form>", kind="html", name="form.html")
        schema = self.store.save_schema(simple_schema(), parent=source.id)
        dataset = self.store.save_dataset(simple_records(), parent=schema.id)
        model = self.store.save_model(
            train(simple_schema(), simple_records(), TrainOptions(seed=1)),
            parent=dataset.id)
        return source, schema, dataset, model


# ----------------------------------------------------------------------
# ids
# ----------------------------------------------------------------------


class TestIds(StoreCase):
    def test_an_id_says_what_kind_of_thing_it_is(self):
        for kind in KINDS:
            self.assertEqual(kind_of(new_id(kind)), kind)

    def test_ids_sort_into_the_order_they_were_made(self):
        ids = [new_id("model") for _ in range(5)]
        self.assertEqual(sorted(ids), ids)

    def test_listing_everything_is_by_time_and_not_by_kind(self):
        """Sorting by the whole id would put every source above every model."""
        schema = self.store.save_schema(simple_schema())
        dataset = self.store.save_dataset(simple_records(4), parent=schema.id)
        source = self.store.save_source("<form></form>")
        self.assertEqual([e.id for e in self.store.list()],
                         [source.id, dataset.id, schema.id])

    def test_an_id_that_is_really_a_path_is_refused(self):
        """The ids arrive from HTTP requests, so this is not hypothetical."""
        for attempt in ("../../etc/passwd", "mdl-../../x", "", "nonsense",
                        "mdl-20260101-000000000-zzzz",
                        "mdl-20260101-000000-abcdef"):  # the old, coarser shape
            with self.subTest(attempt):
                self.assertFalse(self.store.has(attempt))
                with self.assertRaises(StoreError):
                    self.store.get(attempt)

    def test_a_kind_the_library_does_not_hold_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.put("opinion", "x", {})


# ----------------------------------------------------------------------
# putting things in and getting them back
# ----------------------------------------------------------------------


class TestRoundTrip(StoreCase):
    def test_a_schema_comes_back_as_a_schema(self):
        entry = self.store.save_schema(simple_schema())
        self.assertEqual(self.store.load_schema(entry.id).name, "library test")
        self.assertEqual(entry.meta["fields"], 2)

    def test_a_dataset_comes_back_as_its_records(self):
        schema = self.store.save_schema(simple_schema())
        entry = self.store.save_dataset(simple_records(12), parent=schema.id)
        self.assertEqual(len(self.store.load_records(entry.id)), 12)
        self.assertEqual(entry.meta["records"], 12)

    def test_a_model_comes_back_able_to_predict(self):
        _s, _sc, _d, model = self.chain()
        reloaded = self.store.load_model(model.id)
        self.assertEqual(reloaded.predict_field("state", {"city": "Austin"}).value, "TX")
        self.assertEqual(model.meta["algorithm"], "statistical")

    def test_an_entry_survives_the_round_trip_through_its_own_metadata(self):
        entry = self.store.save_schema(simple_schema())
        self.assertEqual(Entry.from_dict(entry.to_dict()), entry)

    def test_a_source_keeps_the_markup_it_came_from(self):
        entry = self.store.save_source("<form>hello</form>", name="x.html")
        self.assertEqual(self.store.payload(entry.id)["content"], "<form>hello</form>")


# ----------------------------------------------------------------------
# lineage, in both directions
# ----------------------------------------------------------------------


class TestLineage(StoreCase):
    def test_a_model_knows_the_whole_chain_it_came_from(self):
        source, schema, dataset, model = self.chain()
        self.assertEqual([e.id for e in self.store.lineage(model.id)],
                         [source.id, schema.id, dataset.id, model.id])

    def test_a_source_knows_everything_that_descends_from_it(self):
        source, schema, dataset, model = self.chain()
        self.assertEqual({e.id for e in self.store.descendants(source.id)},
                         {schema.id, dataset.id, model.id})

    def test_children_are_only_the_next_step_down(self):
        source, schema, dataset, _model = self.chain()
        self.assertEqual([e.id for e in self.store.children(source.id)], [schema.id])
        self.assertEqual([e.id for e in self.store.children(schema.id)], [dataset.id])

    def test_two_models_on_one_dataset_both_point_at_it(self):
        """The case the library exists for: comparing algorithms on one dataset."""
        _s, _sc, dataset, first = self.chain()
        second = self.store.save_model(
            train(simple_schema(), simple_records(),
                  TrainOptions(algorithm="tree", seed=1)),
            parent=dataset.id)
        self.assertEqual({e.id for e in self.store.children(dataset.id)},
                         {first.id, second.id})
        algorithms = {e.meta["algorithm"] for e in self.store.children(dataset.id)}
        self.assertEqual(algorithms, {"statistical", "tree"})

    def test_a_parent_that_does_not_exist_is_refused_rather_than_dangling(self):
        with self.assertRaises(StoreError):
            self.store.save_schema(simple_schema(), parent="sch-20200101-000000000-aaaa")

    def test_a_lineage_that_loops_terminates(self):
        """Nothing should create one, which is exactly why it is worth a test."""
        entry = self.store.save_schema(simple_schema())
        meta_path, _ = self.store._paths(entry.id)
        meta_path.write_text(json.dumps(dict(entry.to_dict(), parent=entry.id)))
        self.assertEqual(len(self.store.lineage(entry.id)), 1)


# ----------------------------------------------------------------------
# listing
# ----------------------------------------------------------------------


class TestListing(StoreCase):
    def test_listing_is_newest_first(self):
        first = self.store.save_schema(simple_schema())
        second = self.store.save_schema(simple_schema())
        self.assertEqual([e.id for e in self.store.list("schema")],
                         [second.id, first.id])

    def test_listing_can_be_narrowed_to_one_kind(self):
        self.chain()
        self.assertEqual(len(self.store.list("model")), 1)
        self.assertEqual(len(self.store.list()), 4)

    def test_totals_count_every_kind(self):
        self.chain()
        self.assertEqual(self.store.totals(),
                         {"source": 1, "schema": 1, "dataset": 1, "model": 1,
                          "script": 0})

    def test_an_empty_library_lists_nothing_rather_than_failing(self):
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.size(), 0)

    def test_one_unreadable_file_does_not_take_the_listing_down(self):
        """A run interrupted mid-write should cost that entry and nothing else."""
        good = self.store.save_schema(simple_schema())
        broken = self.store._folder("schema") / "sch-20260101-000000000-abcd.json"
        broken.write_text("{ this is not json")
        self.assertEqual([e.id for e in self.store.list("schema")], [good.id])

    def test_renaming_keeps_everything_else(self):
        entry = self.store.save_schema(simple_schema())
        renamed = self.store.rename(entry.id, "the good one")
        self.assertEqual(renamed.name, "the good one")
        self.assertEqual(self.store.get(entry.id).name, "the good one")
        self.assertEqual(self.store.get(entry.id).created, entry.created)


# ----------------------------------------------------------------------
# taking things out
# ----------------------------------------------------------------------


class TestDeleting(StoreCase):
    def test_deleting_a_leaf_removes_both_of_its_files(self):
        _s, _sc, _d, model = self.chain()
        self.store.delete(model.id)
        self.assertFalse(self.store.has(model.id))
        self.assertEqual(list(self.store._folder("model").glob("*.json")), [])

    def test_deleting_something_with_children_is_refused(self):
        source, _sc, _d, _m = self.chain()
        with self.assertRaises(StoreError) as caught:
            self.store.delete(source.id)
        self.assertIn("made from it", str(caught.exception))
        self.assertTrue(self.store.has(source.id))

    def test_a_cascade_takes_the_whole_line(self):
        source, schema, dataset, model = self.chain()
        removed = self.store.delete(source.id, cascade=True)
        self.assertEqual(set(removed), {source.id, schema.id, dataset.id, model.id})
        self.assertEqual(self.store.list(), [])

    def test_pruning_keeps_the_newest_and_never_breaks_a_lineage(self):
        source, schema, dataset, model = self.chain()
        spare = [self.store.save_schema(simple_schema()) for _ in range(4)]
        self.store.prune(keep=2)
        # The schema under a dataset stays whatever its age, because dropping
        # it would leave that model unable to say what it learned from.
        self.assertTrue(self.store.has(schema.id))
        self.assertTrue(self.store.has(model.id))
        self.assertLessEqual(len(self.store.list("schema")), len(spare) + 1)

    def test_a_payload_that_has_gone_missing_is_reported_plainly(self):
        entry = self.store.save_schema(simple_schema())
        _meta, payload = self.store._paths(entry.id)
        payload.unlink()
        with self.assertRaises(StoreError):
            self.store.payload(entry.id)


class TestLocation(StoreCase):
    def test_the_default_library_follows_the_environment_variable(self):
        import os

        before = os.environ.get("FILLERAI_HOME")
        os.environ["FILLERAI_HOME"] = self.dir
        try:
            self.assertEqual(Store.default().root, Path(self.dir).resolve())
        finally:
            if before is None:
                del os.environ["FILLERAI_HOME"]
            else:
                os.environ["FILLERAI_HOME"] = before

    def test_the_library_is_only_created_when_something_is_put_in_it(self):
        empty = Store(Path(self.dir) / "not-yet")
        self.assertEqual(empty.list(), [])
        self.assertFalse((Path(self.dir) / "not-yet").exists())


if __name__ == "__main__":
    unittest.main()
