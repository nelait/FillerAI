"""Tests for the database layer.

The interesting part of :mod:`fillerai.db` is not that SQLite works - it is
the promise that a backend can be swapped under the callers. So these cover
the three things that promise rests on: the parameter style is translated,
the migrations are idempotent, and a transaction that fails leaves nothing
behind.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.db import (
    Database,
    DatabaseError,
    SQLiteDatabase,
    connect,
    default_url,
    dumps,
    loads,
)


class TestConnecting(unittest.TestCase):
    def test_a_memory_database_is_ready_to_use(self):
        db = connect("sqlite://:memory:")
        self.addCleanup(db.dispose)
        self.assertEqual(db.version, 1)
        self.assertIn("users", db.tables())
        self.assertIn("entries", db.tables())

    def test_a_file_database_is_created_where_it_was_asked_for(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        target = Path(folder) / "nested" / "fillerai.db"
        db = connect(f"sqlite://{target}")
        self.addCleanup(db.close)
        self.assertTrue(target.is_file())

    def test_a_bare_path_is_taken_as_sqlite(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        db = connect(str(Path(folder) / "plain.db"))
        self.addCleanup(db.close)
        self.assertIsInstance(db, SQLiteDatabase)

    def test_postgres_is_refused_with_the_reason_rather_than_a_traceback(self):
        with self.assertRaises(DatabaseError) as caught:
            connect("postgresql://user@host/fillerai")
        self.assertIn("driver", str(caught.exception))
        self.assertIn("Database", str(caught.exception))

    def test_something_that_is_not_a_database_says_so(self):
        with self.assertRaises(DatabaseError) as caught:
            connect("redis://localhost")
        self.assertIn("redis", str(caught.exception))

    def test_the_default_url_sits_inside_the_library(self):
        self.assertTrue(default_url("/tmp/somewhere").endswith(
            "/tmp/somewhere/fillerai.db"))


class TestMigrations(unittest.TestCase):
    def test_running_again_applies_nothing_and_loses_nothing(self):
        db = connect("sqlite://:memory:")
        self.addCleanup(db.dispose)
        db.execute("INSERT INTO settings (name, value) VALUES (?, ?)", ("a", "1"))
        self.assertEqual(db.migrate(), 0)
        self.assertEqual(db.setting("a"), "1")

    def test_a_database_made_before_the_schema_existed_catches_up(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = Path(folder) / "old.db"
        bare = SQLiteDatabase(path)
        bare.execute("CREATE TABLE unrelated (x TEXT)")
        bare.close()

        db = connect(f"sqlite://{path}")
        self.addCleanup(db.close)
        self.assertEqual(db.version, 1)
        self.assertIn("unrelated", db.tables())
        self.assertIn("users", db.tables())


class TestStatements(unittest.TestCase):
    def setUp(self):
        self.db = connect("sqlite://:memory:")
        self.addCleanup(self.db.dispose)

    def test_query_returns_dictionaries(self):
        self.db.execute("INSERT INTO settings (name, value) VALUES (?, ?)",
                        ("where", "here"))
        self.assertEqual(self.db.query("SELECT name, value FROM settings"),
                         [{"name": "where", "value": "here"}])

    def test_one_returns_none_rather_than_raising(self):
        self.assertIsNone(self.db.one("SELECT * FROM users WHERE id = ?", ("no",)))

    def test_a_setting_can_be_replaced(self):
        self.db.remember("k", "first")
        self.db.remember("k", "second")
        self.assertEqual(self.db.setting("k"), "second")
        self.assertEqual(self.db.count("SELECT COUNT(*) FROM settings"), 1)

    def test_bad_sql_arrives_as_a_database_error(self):
        with self.assertRaises(DatabaseError):
            self.db.query("SELECT * FROM a_table_that_is_not_there")

    def test_a_failed_transaction_leaves_nothing_behind(self):
        with self.assertRaises(DatabaseError):
            with self.db.transaction() as batch:
                batch.execute("INSERT INTO settings (name, value) VALUES (?, ?)",
                              ("half", "written"))
                raise DatabaseError("something went wrong half way")
        self.assertIsNone(self.db.setting("half"))

    def test_the_parameter_style_is_translated_for_a_driver_that_wants_it(self):
        """The one thing a Postgres backend changes, checked without one."""

        class Pyformat(Database):
            paramstyle = "pyformat"

        speaker = Pyformat("postgresql://nowhere")
        self.assertEqual(
            speaker._translate("SELECT * FROM users WHERE id = ? AND role = ?"),
            "SELECT * FROM users WHERE id = %s AND role = %s")

    def test_a_question_mark_inside_a_string_is_left_alone(self):
        class Pyformat(Database):
            paramstyle = "pyformat"

        speaker = Pyformat("postgresql://nowhere")
        self.assertEqual(
            speaker._translate("SELECT '?' AS q WHERE name = ?"),
            "SELECT '?' AS q WHERE name = %s")


class TestThreads(unittest.TestCase):
    def test_every_thread_sees_the_same_memory_database(self):
        """Thread-local connections plus :memory: is the trap this avoids."""
        db = connect("sqlite://:memory:")
        self.addCleanup(db.dispose)
        db.execute("INSERT INTO settings (name, value) VALUES (?, ?)", ("x", "1"))

        seen = []

        def look() -> None:
            seen.append(db.setting("x"))
            db.close()

        threads = [threading.Thread(target=look) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(seen, ["1"] * 4)

    def test_writes_from_several_threads_all_land(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        db = connect(f"sqlite://{Path(folder) / 'busy.db'}")
        self.addCleanup(db.close)

        def write(index: int) -> None:
            db.execute("INSERT INTO settings (name, value) VALUES (?, ?)",
                       (f"k{index}", str(index)))
            db.close()

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(db.count("SELECT COUNT(*) FROM settings"), 8)


class TestJsonHelpers(unittest.TestCase):
    def test_a_value_survives_the_round_trip(self):
        self.assertEqual(loads(dumps({"a": [1, 2]})), {"a": [1, 2]})

    def test_unreadable_json_falls_back_rather_than_raising(self):
        self.assertEqual(loads("{not json", {"safe": True}), {"safe": True})


if __name__ == "__main__":
    unittest.main()
