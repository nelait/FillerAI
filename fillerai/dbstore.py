"""The library, kept in the database instead of in a directory.

:mod:`fillerai.store` holds the same thing in files, and still does: one
person on one machine wants a directory they can ``cat``, and that is worth
keeping. This is what the same library looks like once there is more than one
person, because two of the things a shared tool needs are the two a directory
of JSON cannot give:

- **An owner on every entry.** A library is somebody's working set. Alice's
  fifty models are noise in Bob's list, and a model trained on a form Bob was
  not shown is not Bob's to open.
- **Writes that either happen or do not.** Two files per entry means a
  process that dies between them leaves an entry with no contents. The file
  store survives that by skipping what it cannot read; here the metadata and
  the payload go in under one transaction, so there is no such moment.

Everything else is deliberately the same. The same :class:`~fillerai.store.Entry`,
the same ids, the same methods with the same names, so a caller holding one
of these cannot tell which it has - which is what let the web server keep
every endpoint it already had. The payload lives in its own table for the
reason it lived in its own file: listing a library must not read a hundred
megabytes of models to print a list of names.

The SQL here is the plain subset both SQLite and Postgres accept, for the
reason given in :mod:`fillerai.db` - the swap is meant to be a subclass, not
a rewrite.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database, dumps, loads
from .schema import FormSchema
from .store import (KINDS, Entry, Store, StoreError, kind_of, new_id)
from .train.model import AutofillModel

# Columns in the order the queries below use them. Written once so a listing
# and a lookup cannot drift into disagreeing about what an entry is.
_COLUMNS = "id, owner, kind, name, created, parent, meta, bytes"


def _entry(row: dict[str, Any]) -> Entry:
    return Entry(
        id=str(row["id"]),
        kind=str(row["kind"]),
        name=str(row["name"]),
        created=str(row["created"]),
        parent=row.get("parent") or None,
        meta=loads(str(row.get("meta") or "{}"), {}) or {},
        bytes=int(row.get("bytes") or 0),
    )


class DatabaseStore:
    """One user's library, inside the shared database."""

    def __init__(self, db: Database, owner: str = "") -> None:
        self.db = db
        #: Whose library this is. Empty means nobody's in particular, which
        #: is what running without accounts looks like - one library, no
        #: owner, exactly the single-user tool this started as.
        self.owner = owner or ""

    def for_owner(self, owner: str) -> "DatabaseStore":
        return DatabaseStore(self.db, owner)

    @property
    def root(self) -> str:
        """Where a person should be told their library is."""
        return self.db.url

    # -- writing -----------------------------------------------------------

    def put(self, kind: str, name: str, payload: Any,
            parent: str | None = None, meta: dict[str, Any] | None = None) -> Entry:
        if kind not in KINDS:
            raise StoreError(f"{kind!r} is not something the library holds")
        if parent is not None and not self.has(parent):
            raise StoreError(f"there is nothing in the library called {parent!r}")

        body = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        entry = Entry(
            id=new_id(kind), kind=kind, name=name or "",
            created=_now(), parent=parent, meta=dict(meta or {}),
            bytes=len(body.encode("utf-8")),
        )
        entry.name = entry.name or entry.id
        # One transaction: an entry whose contents never arrived is the
        # failure this storage exists to make impossible.
        with self.db.transaction() as batch:
            batch.execute(
                f"INSERT INTO entries ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, self.owner, entry.kind, entry.name, entry.created,
                 entry.parent, dumps(entry.meta), entry.bytes))
            batch.execute(
                "INSERT INTO payloads (entry_id, owner, body) VALUES (?, ?, ?)",
                (entry.id, self.owner, body))
        return entry

    def rename(self, entry_id: str, name: str) -> Entry:
        entry = self.get(entry_id)
        entry.name = name
        self.db.execute("UPDATE entries SET name = ? WHERE id = ? AND owner = ?",
                        (name, entry_id, self.owner))
        return entry

    # -- reading -----------------------------------------------------------

    def has(self, entry_id: str) -> bool:
        if not _looks_like_id(entry_id):
            return False
        return bool(self.db.one(
            "SELECT id FROM entries WHERE id = ? AND owner = ?",
            (entry_id, self.owner)))

    def get(self, entry_id: str) -> Entry:
        if not _looks_like_id(entry_id):
            raise StoreError(f"{entry_id!r} is not a library id")
        row = self.db.one(
            f"SELECT {_COLUMNS} FROM entries WHERE id = ? AND owner = ?",
            (entry_id, self.owner))
        if row is None:
            raise StoreError(f"there is nothing in the library called {entry_id!r}")
        return _entry(row)

    def payload(self, entry_id: str) -> Any:
        self.get(entry_id)  # so a missing entry and a missing body read alike
        row = self.db.one(
            "SELECT body FROM payloads WHERE entry_id = ? AND owner = ?",
            (entry_id, self.owner))
        if row is None:
            raise StoreError(f"{entry_id!r} has lost its contents")
        return loads(str(row["body"]))

    def list(self, kind: str | None = None, parent: str | None = None,
             limit: int | None = None) -> list[Entry]:
        if kind is not None and kind not in KINDS:
            raise StoreError(f"{kind!r} is not something the library holds")
        sql = f"SELECT {_COLUMNS} FROM entries WHERE owner = ?"
        params: list[Any] = [self.owner]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if parent is not None:
            sql += " AND parent = ?"
            params.append(parent)
        # Ordered by id, which carries the time: the same order the file
        # library lists in, and for the same reason.
        sql += " ORDER BY SUBSTR(id, 5) DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_entry(row) for row in self.db.query(sql, params)]

    def children(self, entry_id: str) -> list[Entry]:
        return self.list(parent=entry_id)

    def lineage(self, entry_id: str) -> list[Entry]:
        """The chain from the root down to this entry, oldest first."""
        chain: list[Entry] = []
        seen: set[str] = set()
        current: str | None = entry_id
        while current and current not in seen:
            seen.add(current)
            try:
                entry = self.get(current)
            except StoreError:
                break
            chain.append(entry)
            current = entry.parent
        chain.reverse()
        return chain

    def descendants(self, entry_id: str) -> list[Entry]:
        out: list[Entry] = []
        frontier = [entry_id]
        while frontier:
            for child in self.children(frontier.pop()):
                out.append(child)
                frontier.append(child.id)
        out.sort(key=lambda e: (e.id[4:22], e.id))
        return out

    # -- removing ----------------------------------------------------------

    def delete(self, entry_id: str, cascade: bool = False) -> list[str]:
        """Remove an entry, and what cannot stand without it.

        Same rule as the file library: children are provenance and block the
        delete, except a script, which describes the run its parent came out
        of and goes with it.
        """
        entry = self.get(entry_id)
        children = self.children(entry_id)
        blocking = [child for child in children if child.kind != "script"]
        if blocking and not cascade:
            raise StoreError(
                f"{entry.name!r} has {len(blocking)} thing(s) made from it; "
                f"delete those first, or ask for a cascade")
        going = (self.descendants(entry_id) if cascade
                 else [c for c in children if c.kind == "script"])
        victims = [e.id for e in going]
        victims.append(entry.id)
        with self.db.transaction() as batch:
            for victim in victims:
                batch.execute(
                    "DELETE FROM payloads WHERE entry_id = ? AND owner = ?",
                    (victim, self.owner))
                batch.execute("DELETE FROM entries WHERE id = ? AND owner = ?",
                              (victim, self.owner))
        return victims

    def prune(self, keep: int = 50) -> list[str]:
        removed: list[str] = []
        for kind in KINDS:
            for entry in self.list(kind)[keep:]:
                if self.children(entry.id):
                    continue
                self.delete(entry.id)
                removed.append(entry.id)
        return removed

    def clear(self) -> None:
        """Empty this owner's library. Everyone else keeps theirs."""
        with self.db.transaction() as batch:
            batch.execute("DELETE FROM payloads WHERE owner = ?", (self.owner,))
            batch.execute("DELETE FROM entries WHERE owner = ?", (self.owner,))

    # -- what the stages store ---------------------------------------------

    def save_source(self, content: str, kind: str = "html",
                    name: str = "form") -> Entry:
        return self.put("source", name, {"content": content, "kind": kind},
                        meta={"kind": kind, "characters": len(content)})

    def save_schema(self, schema: FormSchema, parent: str | None = None) -> Entry:
        return self.put("schema", schema.name or "form", schema.to_dict(),
                        parent=parent,
                        meta={"fields": len(schema.fields),
                              "screens": len(schema.screens)})

    def save_dataset(self, records: list[dict[str, Any]], parent: str,
                     name: str | None = None,
                     meta: dict[str, Any] | None = None) -> Entry:
        info = {"records": len(records)}
        info.update(meta or {})
        return self.put("dataset", name or f"{len(records)} records", records,
                        parent=parent, meta=info)

    def save_model(self, model: AutofillModel, parent: str,
                   name: str | None = None,
                   meta: dict[str, Any] | None = None) -> Entry:
        info = {
            "algorithm": model.algorithm,
            "trained_on": model.trained_on,
            "held_out": model.held_out,
            "rules": len(model.derivations),
        }
        info.update(meta or {})
        return self.put("model", name or f"{model.algorithm} model",
                        model.to_dict(), parent=parent, meta=info)

    def save_script(self, text: str, parent: str, name: str | None = None,
                    meta: dict[str, Any] | None = None) -> Entry:
        info = {"lines": text.count("\n") + 1}
        info.update(meta or {})
        return self.put("script", name or "training script", {"text": text},
                        parent=parent, meta=info)

    # -- reading them back -------------------------------------------------

    def load_schema(self, entry_id: str) -> FormSchema:
        return FormSchema.from_dict(self.payload(entry_id))

    def load_records(self, entry_id: str) -> list[dict[str, Any]]:
        records = self.payload(entry_id)
        if not isinstance(records, list):
            raise StoreError(f"{entry_id!r} does not hold a list of records")
        return records

    def load_model(self, entry_id: str) -> AutofillModel:
        return AutofillModel.from_dict(self.payload(entry_id))

    def load_script(self, entry_id: str) -> str:
        body = self.payload(entry_id)
        return str(body.get("text") or "") if isinstance(body, dict) else str(body)

    # -- summarising -------------------------------------------------------

    def totals(self) -> dict[str, int]:
        counts = {kind: 0 for kind in KINDS}
        for row in self.db.query(
                "SELECT kind, COUNT(*) AS n FROM entries WHERE owner = ? "
                "GROUP BY kind", (self.owner,)):
            if str(row["kind"]) in counts:
                counts[str(row["kind"])] = int(row["n"])
        return counts

    def size(self) -> int:
        return self.db.count(
            "SELECT COALESCE(SUM(bytes), 0) FROM entries WHERE owner = ?",
            (self.owner,))

    # -- bringing something in under the id it already has ------------------

    def _insert(self, entry: Entry, payload: Any, parent: str | None) -> Entry:
        """Store an entry keeping its id. Only :func:`import_store` needs this.

        Separate from :meth:`put` because ``put`` issues an id, and an import
        that issued new ids would break every parent link and every id anyone
        had written down.
        """
        body = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        with self.db.transaction() as batch:
            batch.execute(
                f"INSERT INTO entries ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, self.owner, entry.kind, entry.name, entry.created,
                 parent, dumps(entry.meta), len(body.encode("utf-8"))))
            batch.execute(
                "INSERT INTO payloads (entry_id, owner, body) VALUES (?, ?, ?)",
                (entry.id, self.owner, body))
        return entry

    def owners(self) -> dict[str, int]:
        """How many entries each owner has. For the admin panel, not the user."""
        return {str(row["owner"] or ""): int(row["n"]) for row in self.db.query(
            "SELECT owner, COUNT(*) AS n FROM entries GROUP BY owner")}


# ----------------------------------------------------------------------
# bringing a file library in
# ----------------------------------------------------------------------


def import_store(source: Store, target: DatabaseStore,
                 skip_existing: bool = True) -> dict[str, Any]:
    """Copy a directory library into the database, lineage intact.

    Ids are kept rather than reissued, which is what makes this safe to run
    twice: the second run finds everything already there and copies nothing.
    Keeping the ids also keeps every parent link valid without a translation
    table, and means a link somebody wrote down still resolves.

    Entries are copied parents-first, because an entry whose parent is not in
    yet would be refused - so the oldest first, which is the order lineage
    runs in.
    """
    copied: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for entry in sorted(source.list(), key=lambda e: (e.id[4:22], e.id)):
        if skip_existing and target.has(entry.id):
            skipped.append(entry.id)
            continue
        try:
            body = source.payload(entry.id)
        except StoreError as error:
            failed.append({"id": entry.id, "why": str(error)})
            continue
        # A parent that did not come across - deleted from under its child,
        # or skipped as unreadable - becomes no parent rather than a
        # dangling one. The entry is still worth having.
        parent = entry.parent if entry.parent and target.has(entry.parent) else None
        target._insert(entry, body, parent)
        copied.append(entry.id)

    return {"copied": copied, "skipped": skipped, "failed": failed,
            "from": str(source.root), "into": target.root}


def _looks_like_id(entry_id: str) -> bool:
    try:
        kind_of(str(entry_id or ""))
    except StoreError:
        return False
    return True


def _now() -> str:
    import datetime as dt

    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
