"""The database tests again, against a real Postgres, when there is one.

Skipped unless ``FILLERAI_TEST_POSTGRES_URL`` names a database this run may
write to, and psycopg is installed: neither is part of the standard install.
Every test that would have opened a private in-memory SQLite database gets a
private Postgres schema instead, dropped afterwards, so the same assertions
check the same behaviour on both backends.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fillerai import db as db_module
from fillerai.db import DatabaseError, PostgresDatabase, connect, safe_url

URL = os.environ.get("FILLERAI_TEST_POSTGRES_URL", "")

#: The suites that talk to the database. test_db is left out: most of it is
#: about SQLite itself (files, WAL, shared memory).
MODULES = ["test_auth", "test_dbstore", "test_tokens", "test_web_auth", "test_rest"]

#: Tests that assert the backend is SQLite by name.
SQLITE_ONLY = {"test_web_auth.TestTheDatabasePanel.test_it_says_where_the_data_is"}


class TestWithoutTheDriver(unittest.TestCase):
    def test_a_missing_driver_says_how_to_install_it(self):
        real_import = __import__

        def no_psycopg(name, *args, **kwargs):
            if name == "psycopg" or name.startswith("psycopg."):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch("builtins.__import__", no_psycopg):
            with self.assertRaises(DatabaseError) as caught:
                connect("postgresql://user@host/fillerai")
        self.assertIn("fillerai[postgres]", str(caught.exception))

    def test_passwords_are_kept_out_of_what_is_shown(self):
        self.assertEqual(safe_url("postgresql://me:s3cret@db.internal:5432/app"),
                         "postgresql://me:***@db.internal:5432/app")
        self.assertEqual(safe_url("postgresql://me@db/app"), "postgresql://me@db/app")
        self.assertEqual(safe_url("sqlite:///data/fillerai.db"), "sqlite:///data/fillerai.db")

    def test_a_literal_percent_is_escaped_for_the_driver(self):
        speaker = PostgresDatabase.__new__(PostgresDatabase)
        self.assertEqual(speaker._translate("SELECT '5%' WHERE a = ?"),
                         "SELECT '5%%' WHERE a = %s")


def _schema_url(schema: str) -> str:
    joiner = "&" if "?" in URL else "?"
    return f"{URL}{joiner}options={quote(f'-csearch_path={schema}')}"


class _OnPostgres(unittest.TestSuite):
    """Runs its tests with in-memory SQLite swapped for a Postgres schema."""

    def run(self, result, debug=False):  # noqa: D401
        import psycopg

        real_connect = db_module.connect
        made: list[str] = []
        opened: list[PostgresDatabase] = []

        def on_postgres(url=None, **kwargs):
            if url and url.startswith("sqlite://:memory:"):
                schema = f"t_{uuid.uuid4().hex[:12]}"
                with psycopg.connect(URL, autocommit=True) as admin:
                    admin.execute(f"CREATE SCHEMA {schema}")
                made.append(schema)
                database = real_connect(_schema_url(schema), **kwargs)
                opened.append(database)
                return database
            return real_connect(url, **kwargs)

        patched = []
        for name in MODULES + ["fillerai.web.server"]:
            module = sys.modules.get(name)
            for attr in ("connect", "connect_database"):
                if module is not None and getattr(module, attr, None) is real_connect:
                    patched.append((module, attr))
                    setattr(module, attr, on_postgres)
        try:
            return super().run(result, debug)
        finally:
            for module, attr in patched:
                setattr(module, attr, real_connect)
            for database in opened:
                try:
                    database.dispose()
                except Exception:  # noqa: BLE001
                    pass
            with psycopg.connect(URL, autocommit=True) as admin:
                for schema in made:
                    admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def load_tests(loader, standard, pattern):
    suite = unittest.TestSuite([standard])
    if not URL:
        return suite
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return suite
    live = _OnPostgres()
    for name in MODULES:
        for test in _flatten(loader.loadTestsFromModule(importlib.import_module(name))):
            if test.id() not in SQLITE_ONLY:
                live.addTest(test)
    suite.addTest(live)
    return suite


import unittest.mock  # noqa: E402 - used by the driver test above

if __name__ == "__main__":
    unittest.main()
