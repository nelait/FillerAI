"""Tests for the library kept in the database.

Two things to prove. The first is that it is the same library: the lineage
tests from ``test_store`` should hold word for word against this one, which
is what lets the web server hold either without knowing which. The second is
the part that is new - an owner on every entry, and a write that either
lands whole or not at all.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.db import connect
from fillerai.dbstore import DatabaseStore, import_store
from fillerai.store import KINDS, Store, StoreError


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.db = connect("sqlite://:memory:")
        self.addCleanup(self.db.dispose)
        self.store = DatabaseStore(self.db, "usr-one")
        self.other = DatabaseStore(self.db, "usr-two")

    def chain(self, store=None):
        """A source, a schema under it, and a dataset under that."""
        store = store or self.store
        source = store.save_source("<form></form>", name="claims")
        schema = store.put("schema", "claims", {"version": 1}, parent=source.id)
        dataset = store.put("dataset", "500 records", [{"a": 1}], parent=schema.id)
        return source, schema, dataset


class TestTheSameAsTheFileLibrary(StoreCase):
    def test_what_goes_in_comes_back(self):
        entry = self.store.put("dataset", "some records", [{"a": 1}, {"a": 2}])
        self.assertEqual(self.store.payload(entry.id), [{"a": 1}, {"a": 2}])
        self.assertEqual(self.store.get(entry.id).name, "some records")
        self.assertGreater(self.store.get(entry.id).bytes, 0)

    def test_lineage_runs_from_the_root_down(self):
        source, schema, dataset = self.chain()
        self.assertEqual([e.id for e in self.store.lineage(dataset.id)],
                         [source.id, schema.id, dataset.id])

    def test_descendants_run_the_other_way(self):
        source, schema, dataset = self.chain()
        self.assertEqual({e.id for e in self.store.descendants(source.id)},
                         {schema.id, dataset.id})

    def test_a_parent_that_is_not_there_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.put("schema", "orphan", {}, parent="sch-20260101-000000000-abcd")

    def test_an_id_that_is_really_a_path_is_refused(self):
        for attempt in ("../../etc/passwd", "mdl-../x", "", "nonsense"):
            with self.subTest(attempt):
                self.assertFalse(self.store.has(attempt))
                with self.assertRaises(StoreError):
                    self.store.get(attempt)

    def test_deleting_something_with_children_is_refused_without_a_cascade(self):
        _source, schema, _dataset = self.chain()
        with self.assertRaises(StoreError):
            self.store.delete(schema.id)

    def test_a_cascade_takes_the_whole_line(self):
        source, schema, dataset = self.chain()
        removed = self.store.delete(source.id, cascade=True)
        self.assertEqual(set(removed), {source.id, schema.id, dataset.id})
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.db.count("SELECT COUNT(*) FROM payloads"), 0)

    def test_a_script_goes_with_the_model_it_describes(self):
        _source, _schema, dataset = self.chain()
        model = self.store.put("model", "tree model", {"algorithm": "tree"},
                               parent=dataset.id)
        script = self.store.save_script("print('hello')", parent=model.id)
        removed = self.store.delete(model.id)
        self.assertEqual(set(removed), {model.id, script.id})

    def test_listing_is_newest_first_and_can_be_narrowed(self):
        source, schema, dataset = self.chain()
        self.assertEqual([e.id for e in self.store.list()],
                         [dataset.id, schema.id, source.id])
        self.assertEqual([e.id for e in self.store.list(kind="schema")], [schema.id])
        self.assertEqual([e.id for e in self.store.list(parent=source.id)], [schema.id])
        self.assertEqual(len(self.store.list(limit=2)), 2)

    def test_totals_count_every_kind(self):
        self.chain()
        self.assertEqual(self.store.totals(),
                         {"source": 1, "schema": 1, "dataset": 1, "model": 0,
                          "script": 0})

    def test_an_empty_library_lists_nothing_rather_than_failing(self):
        self.assertEqual(self.other.list(), [])
        self.assertEqual(self.other.size(), 0)

    def test_renaming_keeps_everything_else(self):
        entry = self.store.put("dataset", "before", [{"a": 1}])
        renamed = self.store.rename(entry.id, "after")
        self.assertEqual(renamed.name, "after")
        self.assertEqual(self.store.payload(entry.id), [{"a": 1}])

    def test_prune_keeps_the_newest_and_never_breaks_a_line(self):
        _source, schema, _dataset = self.chain()
        spare = [self.store.put("schema", f"s{i}", {}) for i in range(4)]
        removed = self.store.prune(keep=2)
        self.assertNotIn(schema.id, removed)
        self.assertIn(spare[0].id, removed)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.put("sandwich", "lunch", {})
        with self.assertRaises(StoreError):
            self.store.list(kind="sandwich")


class TestOwners(StoreCase):
    def test_one_persons_entries_are_invisible_to_another(self):
        mine = self.store.save_source("<form></form>")
        self.assertFalse(self.other.has(mine.id))
        self.assertEqual(self.other.list(), [])
        with self.assertRaises(StoreError):
            self.other.payload(mine.id)
        with self.assertRaises(StoreError):
            self.other.delete(mine.id)

    def test_totals_and_size_are_each_persons_own(self):
        self.chain(self.store)
        self.other.save_source("<form></form>")
        self.assertEqual(self.store.totals()["schema"], 1)
        self.assertEqual(self.other.totals()["schema"], 0)
        self.assertGreater(self.store.size(), self.other.size())

    def test_clearing_one_library_leaves_the_others(self):
        self.chain(self.store)
        kept = self.other.save_source("<form></form>")
        self.store.clear()
        self.assertEqual(self.store.list(), [])
        self.assertTrue(self.other.has(kept.id))

    def test_the_admin_view_counts_every_owner(self):
        self.chain(self.store)
        self.other.save_source("<form></form>")
        self.assertEqual(DatabaseStore(self.db).owners(),
                         {"usr-one": 3, "usr-two": 1})

    def test_an_unowned_library_is_its_own_library(self):
        """What a --no-auth server writes into: nobody's, not everybody's."""
        nobody = DatabaseStore(self.db)
        entry = nobody.save_source("<form></form>")
        self.assertTrue(nobody.has(entry.id))
        self.assertFalse(self.store.has(entry.id))


class TestImporting(unittest.TestCase):
    def setUp(self):
        self.db = connect("sqlite://:memory:")
        self.addCleanup(self.db.dispose)
        self.folder = tempfile.mkdtemp(prefix="fillerai-import-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.files = Store(self.folder)
        self.target = DatabaseStore(self.db, "usr-one")

        self.source = self.files.save_source("<form></form>", name="claims")
        self.schema = self.files.put("schema", "claims", {"version": 1},
                                     parent=self.source.id)
        self.dataset = self.files.put("dataset", "500", [{"a": 1}],
                                      parent=self.schema.id)

    def test_everything_comes_across_with_its_id_and_its_lineage(self):
        result = import_store(self.files, self.target)
        self.assertEqual(len(result["copied"]), 3)
        self.assertEqual([e.id for e in self.target.lineage(self.dataset.id)],
                         [self.source.id, self.schema.id, self.dataset.id])
        self.assertEqual(self.target.payload(self.dataset.id), [{"a": 1}])
        self.assertEqual(self.target.get(self.schema.id).name, "claims")

    def test_running_it_twice_copies_nothing_the_second_time(self):
        import_store(self.files, self.target)
        again = import_store(self.files, self.target)
        self.assertEqual(again["copied"], [])
        self.assertEqual(len(again["skipped"]), 3)
        self.assertEqual(len(self.target.list()), 3)

    def test_an_unreadable_entry_is_reported_and_the_rest_still_arrive(self):
        payload = Path(self.folder) / "datasets" / f"{self.dataset.id}.payload.json"
        payload.unlink()
        result = import_store(self.files, self.target)
        self.assertEqual([f["id"] for f in result["failed"]], [self.dataset.id])
        self.assertEqual(len(result["copied"]), 2)
        self.assertTrue(self.target.has(self.schema.id))

    def test_two_people_can_import_the_same_directory(self):
        second = DatabaseStore(self.db, "usr-two")
        import_store(self.files, self.target)
        import_store(self.files, second)
        self.assertEqual(len(second.list()), 3)
        self.assertEqual(len(self.target.list()), 3)

    def test_the_file_library_is_left_alone(self):
        import_store(self.files, self.target)
        self.assertEqual(len(self.files.list()), 3)
        self.assertEqual(self.files.payload(self.dataset.id), [{"a": 1}])


class TestEveryKindRoundTrips(StoreCase):
    def test_each_kind_can_be_stored_and_read_back(self):
        parent = None
        for kind in KINDS:
            with self.subTest(kind):
                entry = self.store.put(kind, kind, {"kind": kind}, parent=parent)
                self.assertEqual(self.store.payload(entry.id), {"kind": kind})
                parent = entry.id


if __name__ == "__main__":
    unittest.main()
