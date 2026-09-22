"""The database: one file, a handful of tables, and a door left open.

Until now everything FillerAI kept lived in a directory of JSON. That was the
right answer for one person on one machine, and it is the wrong answer the
moment there is a second person: there is nowhere to put a user, nothing to
scope a library to, and no way to ask a question across the whole set that is
not a directory walk.

So there is a database. It is SQLite, which is in the standard library, so
the promise the rest of this project makes - nothing to install, nothing that
leaves the machine - survives the change. One file, created on first use,
that a person can open with ``sqlite3`` and read.

**Why this module exists at all.** Every caller could import ``sqlite3`` and
be done. Then swapping in Postgres later, which is the stated plan, would
mean touching every caller. Instead everything goes through :class:`Database`,
and what is actually specific to a backend is small enough to name:

- **The parameter style.** Callers write ``?``. A backend that wants ``%s``
  rewrites the statement on its way through, so nothing above here knows.
- **Connecting.** A URL in, a DB-API connection out.
- **Nothing else.** The DDL in :data:`MIGRATIONS` is written in the SQL both
  SQLite and Postgres accept - ``TEXT``, ``INTEGER``, ``CREATE TABLE IF NOT
  EXISTS`` - and times are stored as ISO 8601 strings rather than as a
  timestamp type, because the two disagree about timezones in a way that is
  not worth a translation layer. So a Postgres backend is one subclass, not a
  rewrite.

**One connection per thread.** The web server is threaded, and a SQLite
connection is not safe to share across threads. Rather than serialise every
request behind one lock, each thread opens its own and keeps it. Write-ahead
logging lets those readers run while a write is in flight, and a busy timeout
covers the moment two writers collide - which, at one person clicking
buttons, is already an unlikely afternoon.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

#: Where the database lives when nobody says otherwise. Read at connect time
#: rather than at import, so a test that sets it is not fighting import order.
URL_VARIABLE = "FILLERAI_DATABASE_URL"

#: The file inside the library directory, so a project's database travels
#: with the project exactly as its ``.fillerai`` directory already did.
DEFAULT_FILENAME = "fillerai.db"


class DatabaseError(Exception):
    """The database could not do what was asked of it."""


# ----------------------------------------------------------------------
# the schema, as a list of steps
# ----------------------------------------------------------------------
#
# Each step is (version, [statements]). Steps are applied in order, once, and
# recorded, so a database made by an older build catches up on start rather
# than being recreated. Never edit a step that has shipped: add another.
#
# Deliberately plain SQL. No triggers, no views, no stored defaults beyond
# NULL: everything the application means is written in the application, where
# it can be read and tested, rather than half here and half there.

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            display_name  TEXT NOT NULL,
            role          TEXT NOT NULL,
            active        INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            must_change   INTEGER NOT NULL,
            created       TEXT NOT NULL,
            last_login    TEXT,
            failures      INTEGER NOT NULL,
            locked_until  TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id        TEXT PRIMARY KEY,
            user_id   TEXT NOT NULL,
            csrf      TEXT NOT NULL,
            created   TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            expires   TEXT NOT NULL,
            agent     TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id)",
        # An entry is named by its owner and its id together, not by its id
        # alone. Two people who import the same directory library are meant
        # to end up with the same ids - the ids are what keep their lineage
        # and any link either of them wrote down - and neither of them is
        # meant to be able to reach the other's copy.
        """
        CREATE TABLE IF NOT EXISTS entries (
            id      TEXT NOT NULL,
            owner   TEXT NOT NULL,
            kind    TEXT NOT NULL,
            name    TEXT NOT NULL,
            created TEXT NOT NULL,
            parent  TEXT,
            meta    TEXT NOT NULL,
            bytes   INTEGER NOT NULL,
            PRIMARY KEY (id, owner)
        )
        """,
        "CREATE INDEX IF NOT EXISTS entries_by_owner ON entries (owner, kind)",
        "CREATE INDEX IF NOT EXISTS entries_by_parent ON entries (parent)",
        # The payload is a second table rather than a column on the first,
        # for the same reason the file library used two files: listing the
        # library must not drag a hundred megabytes of models through memory
        # to show a list of names.
        """
        CREATE TABLE IF NOT EXISTS payloads (
            entry_id TEXT NOT NULL,
            owner    TEXT NOT NULL,
            body     TEXT NOT NULL,
            PRIMARY KEY (entry_id, owner)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            name  TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ]),
    # Credentials for an application rather than a person: see
    # :mod:`fillerai.tokens` for why the secret is hashed differently from a
    # password, and why the id travels alongside it.
    (2, [
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            secret_hash TEXT NOT NULL,
            created     TEXT NOT NULL,
            last_used   TEXT,
            expires     TEXT,
            model_id    TEXT,
            revoked     INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS tokens_by_user ON api_tokens (user_id)",
    ]),
]


# ----------------------------------------------------------------------
# the interface every backend meets
# ----------------------------------------------------------------------


class Database:
    """A connection to the one database, and the little callers need of it.

    Callers write ``?`` for a parameter and get dictionaries back. That is the
    whole contract, and it is the whole reason this class exists: a backend
    can change under it without a single query above it changing.
    """

    #: What the driver wants. Statements arrive with ``?``; this is what they
    #: leave as.
    paramstyle = "qmark"

    def __init__(self, url: str) -> None:
        self.url = url
        self._local = threading.local()

    # -- what a backend supplies ------------------------------------------

    def _connect(self) -> Any:
        raise NotImplementedError

    def _translate(self, sql: str) -> str:
        """Rewrite ``?`` placeholders for a driver that wants something else."""
        if self.paramstyle == "qmark":
            return sql
        out, quoted = [], False
        for char in sql:
            if char == "'":
                quoted = not quoted
            if char == "?" and not quoted:
                out.append("%s")
            else:
                out.append(char)
        return "".join(out)

    # -- connections -------------------------------------------------------

    @property
    def connection(self) -> Any:
        """This thread's connection, opened the first time it is asked for."""
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._connect()
            self._local.connection = existing
        return existing

    def close(self) -> None:
        """Close this thread's connection. Others keep theirs."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None

    # -- running statements ------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one statement and commit it. Returns the rows affected."""
        connection = self.connection
        try:
            cursor = connection.execute(self._translate(sql), tuple(params))
            connection.commit()
            return cursor.rowcount
        except Exception as error:  # noqa: BLE001 - re-raised as ours
            connection.rollback()
            raise DatabaseError(str(error)) from error

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Every row the statement selects, as dictionaries."""
        try:
            cursor = self.connection.execute(self._translate(sql), tuple(params))
            columns = [c[0] for c in cursor.description or ()]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as error:  # noqa: BLE001 - re-raised as ours
            raise DatabaseError(str(error)) from error

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        """The first row, or ``None``. The common case, written once."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def count(self, sql: str, params: Sequence[Any] = ()) -> int:
        row = self.one(sql, params)
        return int(next(iter(row.values()))) if row else 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Several statements that must land together, or not at all.

        Used where a half-done change would be worse than no change: deleting
        an entry and its payload, or a user and everything scoped to them.
        """
        connection = self.connection
        try:
            yield _Batch(self, connection)
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    # -- small stored values -----------------------------------------------

    def setting(self, name: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE name = ?", (name,))
        return row["value"] if row else default

    def remember(self, name: str, value: str) -> None:
        """Store a setting, replacing any previous value.

        Written as a delete and an insert rather than an upsert: the two
        backends spell upsert differently, and this is not a hot path.
        """
        with self.transaction() as batch:
            batch.execute("DELETE FROM settings WHERE name = ?", (name,))
            batch.execute("INSERT INTO settings (name, value) VALUES (?, ?)",
                          (name, value))

    # -- setting up --------------------------------------------------------

    def migrate(self) -> int:
        """Bring the database up to date. Safe to call on every start."""
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, applied TEXT NOT NULL)"
        )
        done = {row["version"] for row in self.query("SELECT version FROM schema_version")}
        applied = 0
        for version, statements in MIGRATIONS:
            if version in done:
                continue
            with self.transaction() as batch:
                for statement in statements:
                    batch.execute(statement)
                batch.execute(
                    "INSERT INTO schema_version (version, applied) VALUES (?, ?)",
                    (version, _now()),
                )
            applied += 1
        return applied

    @property
    def version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] is not None else 0

    def tables(self) -> list[str]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """What to print when someone asks where their data actually is."""
        return {
            "url": self.url,
            "backend": self.__class__.__name__,
            "version": self.version,
            "tables": self.tables(),
        }


class _Batch:
    """Statements inside a :meth:`Database.transaction`, without a commit."""

    def __init__(self, database: Database, connection: Any) -> None:
        self._database = database
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._connection.execute(
            self._database._translate(sql), tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.execute(sql, params)
        columns = [c[0] for c in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ----------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------


class SQLiteDatabase(Database):
    """The one that ships. A file, or shared memory for tests."""

    paramstyle = "qmark"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.memory = self.path in (":memory:", "")
        if self.memory:
            # Thread-local connections plus a private in-memory database is
            # one empty database per thread. A shared-cache URI makes them
            # the same database; the holder below keeps it alive, since the
            # last connection closing would take the data with it.
            self._uri = f"file:fillerai-{uuid.uuid4().hex}?mode=memory&cache=shared"
            super().__init__("sqlite://:memory:")
            self._holder = self._connect()
        else:
            self._uri = self.path
            super().__init__(f"sqlite://{self.path}")
            self._holder = None
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._uri, uri=self.memory, timeout=10.0, isolation_level="DEFERRED")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if not self.memory:
            # Readers and one writer at the same time, which is exactly the
            # shape of a browser polling a training log while the run writes.
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def tables(self) -> list[str]:
        return [row["name"] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]

    def close(self) -> None:
        super().close()

    def dispose(self) -> None:
        """Close everything, including the holder. Tests want this."""
        self.close()
        if self._holder is not None:
            self._holder.close()
            self._holder = None


# ----------------------------------------------------------------------
# choosing one
# ----------------------------------------------------------------------


def default_url(library_root: str | Path | None = None) -> str:
    """Where the database is when nobody passed a URL."""
    stated = os.environ.get(URL_VARIABLE)
    if stated:
        return stated
    root = Path(library_root) if library_root else Path.cwd() / ".fillerai"
    return f"sqlite://{root / DEFAULT_FILENAME}"


def connect(url: str | None = None, *, library_root: str | Path | None = None,
            migrate: bool = True) -> Database:
    """Open the database a URL names, creating and updating it as needed.

    Accepted: ``sqlite://<path>``, ``sqlite://:memory:``, or a bare filesystem
    path. ``postgresql://...`` is recognised and refused with the reason,
    because "no driver" read as a stack trace helps nobody - the shape for it
    is here, the driver is the part that is not in the standard library.
    """
    url = url or default_url(library_root)
    scheme, _, rest = url.partition("://")
    if not rest and "://" not in url:
        scheme, rest = "sqlite", url

    if scheme == "sqlite":
        database = SQLiteDatabase(rest)
    elif scheme in ("postgres", "postgresql"):
        raise DatabaseError(
            "Postgres needs a driver, and FillerAI has no dependencies. "
            "The storage interface is ready for it: subclass Database with "
            "paramstyle 'pyformat' and a _connect that returns a psycopg "
            "connection, and nothing above fillerai.db has to change."
        )
    else:
        raise DatabaseError(
            f"{scheme!r} is not a database FillerAI knows; "
            f"use sqlite://<path> or a plain file path"
        )

    if migrate:
        database.migrate()
    return database


def _now() -> str:
    import datetime as dt

    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(text: str, fallback: Any = None) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback
