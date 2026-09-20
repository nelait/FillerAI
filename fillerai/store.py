"""The library: everything a run produced, and what it came from.

Until now each stage handed its output to the next one and then forgot it.
That is fine for one pass and useless for the way the work actually goes -
generate a thousand records, train four models on them, come back tomorrow
and want the one that was good. There was nothing to come back *to*: the
model lived in the browser tab.

So every stage writes what it produced here, and every entry records what it
was made from. A model points at the dataset it learned from, the dataset
points at the schema it was generated for, and the schema points at the
markup or field spec it was read out of. Follow the chain up and you have
the provenance of any model in the library; follow it down from a source and
you have every model that descends from it. That is the going back and forth:
the same links read in either direction.

**This one is files.** One directory per kind, two files per entry: a small
one with the metadata, a large one with the payload. Listing the library
reads only the small ones, so opening it stays instant with a thousand
entries. There is no index to rebuild, no lock to take, no schema to migrate,
and a person can go and look at any of it with ``cat``.

**There is a second implementation**, :class:`fillerai.dbstore.DatabaseStore`,
which is this same library inside the database, with an owner on every entry.
It exists because the moment there are accounts, a library has to belong to
somebody. This one stays because it is still the right answer for one person
on one machine, it is what the CLI uses by default, and it is what an
existing ``.fillerai`` directory already is - ``fillerai db import`` copies
one into the other, ids and lineage intact.

**What this one deliberately does not do.** No owners, no search, no tags, no
concurrent writers, no garbage collection beyond :meth:`Store.prune`. Every
one of those would be a good idea in a shared service, which is what the
database is for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

from .schema import FormSchema
from .train.model import AutofillModel

#: The kinds of thing the library holds, in the order the stages produce them.
#: A script hangs off the model it would produce rather than off the dataset,
#: because the question a person arrives with is "what did that run do", and
#: the run is the model.
KINDS = ("source", "schema", "dataset", "model", "script")

_PREFIX = {"source": "src", "schema": "sch", "dataset": "dat", "model": "mdl",
           "script": "scr"}
_FOLDER = {kind: kind + "s" for kind in KINDS}

# An id becomes a filename, and ids arrive from HTTP requests, so what counts
# as one is stated here rather than assumed. Anything that does not match is
# refused before it can be joined to a path.
#
# The time carries milliseconds because two runs a second apart is not the
# case that matters - two models trained back to back on the same dataset is,
# and to the second those two sort by their random tail, which is to say not
# in any order at all.
_ID = re.compile(r"^(src|sch|dat|mdl|scr)-\d{8}-\d{9}-[0-9a-f]{4}$")

#: Where the time sits inside an id, so a listing can sort by it. Sorting by
#: the whole id would sort by the kind prefix first, which puts every source
#: above every model and has nothing to do with when anything was made.
_WHEN = slice(4, 22)

# Where the library lives when nobody says. A dot-directory beside the work,
# so a project's library travels with the project, with the environment
# variable for anyone who would rather keep one library for everything.
DEFAULT_DIRNAME = ".fillerai"
HOME_VARIABLE = "FILLERAI_HOME"


class StoreError(Exception):
    """Something was asked for that the library does not have."""


@dataclass
class Entry:
    """One thing in the library, and where it came from."""

    id: str
    kind: str
    name: str
    created: str  # ISO 8601, local time with an offset
    parent: str | None = None
    #: Whatever the stage that wrote it thought was worth showing in a list -
    #: the record count, the algorithm, the accuracy. Free-form on purpose:
    #: the library does not need to understand the stages to hold their work.
    meta: dict[str, Any] = dc_field(default_factory=dict)
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "created": self.created,
            "parent": self.parent,
            "meta": dict(self.meta),
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            name=str(data.get("name") or data["id"]),
            created=str(data.get("created") or ""),
            parent=data.get("parent") or None,
            meta=dict(data.get("meta") or {}),
            bytes=int(data.get("bytes", 0)),
        )

    def when(self) -> str:
        """The creation time as something to put in a table."""
        try:
            return dt.datetime.fromisoformat(self.created).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.created


_STAMP = "%Y%m%d-%H%M%S%f"
_ID_LOCK = threading.Lock()
_LAST_STAMP = ""


def new_id(kind: str) -> str:
    """A sortable, readable id. Sortable matters: the library lists by it.

    Two entries written in the same millisecond would otherwise fall back on
    their random tails and list in no order at all - and back to back is
    exactly how they arrive, because training four algorithms on one dataset
    is the thing the library is for. So the clock is only the starting point:
    within a process each id is forced past the last one, a millisecond at a
    time. Across processes the clock is enough, since two of them writing
    into one library in the same millisecond is not a case a local tool has.
    """
    global _LAST_STAMP

    if kind not in _PREFIX:
        raise StoreError(f"{kind!r} is not something the library holds")
    with _ID_LOCK:
        stamp = dt.datetime.now().strftime(_STAMP)[:-3]
        if stamp <= _LAST_STAMP:
            moment = dt.datetime.strptime(_LAST_STAMP, _STAMP)
            stamp = (moment + dt.timedelta(milliseconds=1)).strftime(_STAMP)[:-3]
        _LAST_STAMP = stamp
    return f"{_PREFIX[kind]}-{stamp}-{uuid.uuid4().hex[:4]}"


def kind_of(entry_id: str) -> str:
    for kind, prefix in _PREFIX.items():
        if entry_id.startswith(prefix + "-"):
            return kind
    raise StoreError(f"{entry_id!r} is not a library id")


class Store:
    """A directory of runs, their inputs and their outputs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    # -- locating ----------------------------------------------------------

    @classmethod
    def default(cls) -> "Store":
        return cls(os.environ.get(HOME_VARIABLE) or Path.cwd() / DEFAULT_DIRNAME)

    def _folder(self, kind: str) -> Path:
        if kind not in _FOLDER:
            raise StoreError(f"{kind!r} is not something the library holds")
        return self.root / _FOLDER[kind]

    def _paths(self, entry_id: str) -> tuple[Path, Path]:
        """The metadata file and the payload file for an id.

        The id is checked against a pattern first. It arrives from an HTTP
        request, and "join whatever came in to a directory" is how a local
        tool ends up reading the rest of the disk.
        """
        if not _ID.match(entry_id or ""):
            raise StoreError(f"{entry_id!r} is not a library id")
        folder = self._folder(kind_of(entry_id))
        return folder / f"{entry_id}.json", folder / f"{entry_id}.payload.json"

    # -- writing -----------------------------------------------------------

    def put(self, kind: str, name: str, payload: Any,
            parent: str | None = None, meta: dict[str, Any] | None = None) -> Entry:
        """Store one thing, and say what it was made from."""
        if parent is not None and not self.has(parent):
            # A dangling parent would quietly break every lineage that runs
            # through it, and the caller always knows better than this does.
            raise StoreError(f"there is nothing in the library called {parent!r}")

        entry_id = new_id(kind)
        folder = self._folder(kind)
        folder.mkdir(parents=True, exist_ok=True)
        meta_path, payload_path = self._paths(entry_id)

        body = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        payload_path.write_text(body, encoding="utf-8")

        entry = Entry(
            id=entry_id, kind=kind, name=name or entry_id,
            created=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            parent=parent, meta=dict(meta or {}),
            bytes=len(body.encode("utf-8")),
        )
        meta_path.write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return entry

    def rename(self, entry_id: str, name: str) -> Entry:
        entry = self.get(entry_id)
        entry.name = name
        meta_path, _ = self._paths(entry_id)
        meta_path.write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return entry

    # -- reading -----------------------------------------------------------

    def has(self, entry_id: str) -> bool:
        try:
            meta_path, _ = self._paths(entry_id)
        except StoreError:
            return False
        return meta_path.is_file()

    def get(self, entry_id: str) -> Entry:
        meta_path, _ = self._paths(entry_id)
        if not meta_path.is_file():
            raise StoreError(f"there is nothing in the library called {entry_id!r}")
        return Entry.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))

    def payload(self, entry_id: str) -> Any:
        _, payload_path = self._paths(entry_id)
        if not payload_path.is_file():
            raise StoreError(f"{entry_id!r} has lost its contents")
        return json.loads(payload_path.read_text(encoding="utf-8"))

    def list(self, kind: str | None = None, parent: str | None = None,
             limit: int | None = None) -> list[Entry]:
        """Entries newest first, optionally of one kind or under one parent."""
        kinds = [kind] if kind else list(KINDS)
        entries: list[Entry] = []
        for one in kinds:
            folder = self._folder(one)
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                if path.name.endswith(".payload.json"):
                    continue
                try:
                    entries.append(
                        Entry.from_dict(json.loads(path.read_text(encoding="utf-8")))
                    )
                except (ValueError, KeyError, OSError):
                    # One unreadable file should not take the library down.
                    # A local tool is expected to survive a half-written
                    # entry from a run that was interrupted.
                    continue
        if parent is not None:
            entries = [e for e in entries if e.parent == parent]
        entries.sort(key=lambda e: (e.id[_WHEN], e.id), reverse=True)
        return entries[:limit] if limit else entries

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
        """Everything made from this entry, at any remove."""
        out: list[Entry] = []
        frontier = [entry_id]
        while frontier:
            current = frontier.pop()
            for child in self.children(current):
                out.append(child)
                frontier.append(child.id)
        out.sort(key=lambda e: (e.id[_WHEN], e.id))
        return out

    # -- removing ----------------------------------------------------------

    def delete(self, entry_id: str, cascade: bool = False) -> list[str]:
        """Remove an entry, and optionally everything descended from it.

        Without ``cascade`` an entry with children is refused rather than
        orphaning them: a model whose dataset has vanished can no longer say
        what it learned from, which is the one thing the library is for.

        A script is the exception, and goes with its parent. It describes the
        run that produced that model rather than being something made from
        it, nothing descends from it, and a script for a model that is gone
        is not provenance anybody can use.
        """
        entry = self.get(entry_id)
        children = self.children(entry_id)
        blocking = [child for child in children if child.kind != "script"]
        if blocking and not cascade:
            raise StoreError(
                f"{entry.name!r} has {len(blocking)} thing(s) made from it; "
                f"delete those first, or ask for a cascade"
            )
        going = (self.descendants(entry_id) if cascade
                 else [c for c in children if c.kind == "script"])
        removed = []
        for victim in going + [entry]:
            meta_path, payload_path = self._paths(victim.id)
            meta_path.unlink(missing_ok=True)
            payload_path.unlink(missing_ok=True)
            removed.append(victim.id)
        return removed

    def prune(self, keep: int = 50) -> list[str]:
        """Drop the oldest entries of each kind past ``keep``.

        Leaf-first and never across a lineage: an old schema with a current
        model under it stays, because dropping it would break that model's
        provenance for the sake of a few kilobytes.
        """
        removed: list[str] = []
        for kind in KINDS:
            entries = self.list(kind)
            for entry in entries[keep:]:
                if self.children(entry.id):
                    continue
                self.delete(entry.id)
                removed.append(entry.id)
        return removed

    def clear(self) -> None:
        """Empty the library. Only used by tests and by an explicit ask."""
        if self.root.is_dir():
            shutil.rmtree(self.root)

    # -- what the stages store ---------------------------------------------

    def save_source(self, content: str, kind: str = "html",
                    name: str = "form") -> Entry:
        return self.put(
            "source", name, {"content": content, "kind": kind},
            meta={"kind": kind, "characters": len(content)},
        )

    def save_schema(self, schema: FormSchema, parent: str | None = None) -> Entry:
        return self.put(
            "schema", schema.name or "form", schema.to_dict(), parent=parent,
            meta={"fields": len(schema.fields), "screens": len(schema.screens)},
        )

    def save_dataset(self, records: list[dict[str, Any]], parent: str,
                     name: str | None = None,
                     meta: dict[str, Any] | None = None) -> Entry:
        info = {"records": len(records)}
        info.update(meta or {})
        return self.put("dataset", name or f"{len(records)} records", records,
                        parent=parent, meta=info)

    def save_script(self, text: str, parent: str, name: str | None = None,
                    meta: dict[str, Any] | None = None) -> Entry:
        """The Python a training run is, kept beside what it produced.

        The script is generated from the options anyway, so storing it looks
        redundant until somebody changes a default: then the script in the
        library is what that model was actually trained by, and the one the
        code would generate today is not.
        """
        info = {"lines": text.count("\n") + 1}
        info.update(meta or {})
        return self.put("script", name or "training script", {"text": text},
                        parent=parent, meta=info)

    def load_script(self, entry_id: str) -> str:
        body = self.payload(entry_id)
        if isinstance(body, dict):
            return str(body.get("text") or "")
        return str(body)

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
        return self.put(
            "model", name or f"{model.algorithm} model", model.to_dict(),
            parent=parent, meta=info,
        )

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

    # -- summarising -------------------------------------------------------

    def totals(self) -> dict[str, int]:
        return {kind: len(self.list(kind)) for kind in KINDS}

    def size(self) -> int:
        return sum(entry.bytes for entry in self.list())


def summarise(entries: Iterable[Entry]) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in entries]
