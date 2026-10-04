"""A local HTTP server for the AIrForms UI.

Built on ``http.server`` so the UI inherits the same promise as the rest of
the project: nothing to install, nothing that phones home. It is a local
working tool, not a public service, so it binds to the loopback interface by
default and says so loudly if asked to do otherwise.

The API is deliberately thin. Every endpoint is a direct call into the same
functions ``fillerai.cli`` uses, which is what keeps the UI and the CLI from
drifting apart as the later phases land.

**There are two HTTP surfaces here, and they are not the same thing.**
``/api`` is the UI talking to its own server: a session cookie, a CSRF
header, one POST per button. ``/v1`` is the integration API in
:mod:`fillerai.web.rest`, for somebody else's application: bearer tokens,
no cookies, and a version number that has to change before it does. This
module routes both and shares nothing between them but the library
underneath.

**Accounts are optional and on by default.** ``fillerai serve`` opens a
database, makes an administrator if there is nobody, and asks for a login;
``--no-auth`` skips all of it and is the single-user tool this started as.
Both paths run the same endpoints - what changes is which library
:func:`library` hands back, and whether :meth:`Handler._guard` lets the
request through at all.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import random
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import __version__, extract_html, extract_spec, realdata
from ..auth import (
    COOKIE, ROLES, Auth, AuthError, Session, User, hash_password,
    suggest_password, verify_password,
)
from ..db import Database, connect as connect_database, safe_url
from ..dbstore import DatabaseStore, import_store
from ..extract import html_form, spec as spec_loader
from ..generate.dataset import Dataset, Options, coherence_report, generate, validate
from ..infer import infer
from ..schema import SEMANTIC_TYPES, FormSchema
from ..simulate.effort import DEFAULT_EFFORT
from ..simulate.form import layout as form_layout
from ..simulate.run import run as simulate, sweep as simulate_many
from ..store import KINDS, Store, StoreError
from ..tokens import MAX_NAME as MAX_TOKEN_NAME, TokenError, Tokens
from ..train import algos, features, script as script_writer
from ..train.evaluate import evaluate, suggest_seed_fields
from ..train.model import ACCEPT_ABOVE, AutofillModel, TrainOptions, split_records, train
from ..train.trace import Trace
from ..bot import TemplateError
from ..bot import template as bot_templates
from ..bot.template import Template
from . import botrest, docs as docs_site, rest
from .keyring import SINGLE_USER, Keyring

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
#: The JavaScript client and its demo page. Inside the package rather than
#: beside the repository, because an application is meant to fetch the client
#: from the server it is going to talk to - which only works if a pip install
#: carries it. Served at /client/, which is the documented path; /static/ can
#: reach the same files and nothing says so.
CLIENT_DIR = STATIC_DIR / "client"

# A local tool still needs limits: a runaway request should fail cleanly
# rather than exhaust the process.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 5000
# Rows of a cleaning preview sent back before the records are saved.
PREVIEW_ROWS = 100

# Trained models are held here rather than shipped to the browser and posted
# back on every keystroke: a model over a dense form is a few hundred
# kilobytes, and the Train panel predicts as the user types. The cache is
# deliberately tiny and in-process - this is one person's local tool, the
# models are cheap to rebuild, and a bound that cannot grow is worth more
# than a cache that never misses. A miss is reported as something the UI can
# act on, not as an error.
MAX_CACHED_MODELS = 4

# Training runs kept for their logs after they finish, so a user who looks
# away and comes back still has the run in front of them.
MAX_KEPT_RUNS = 6
# Log lines returned in one poll. A fit over a wide form emits thousands; the
# UI wants them in order and does not want one reply holding all of them.
MAX_LOG_LINES = 400
#: Long enough for any key either service issues, short enough that a paste
#: of the wrong thing is refused before it is stored.
MAX_KEY_CHARS = 512
MAX_MODEL_CHARS = 120
#: What a proposal run may cost before the server refuses it. The UI shows
#: the estimate first, so this is a backstop rather than a surprise.
DEFAULT_MAX_SPEND = 1.0
# How much of an over-long body to read and throw away so the client can
# actually receive the error. Past this the connection is simply closed.
DRAIN_LIMIT = 2 * MAX_BODY_BYTES
DRAIN_CHUNK = 64 * 1024


#: The file library, used when the server is running without a database -
#: which is what ``--no-auth`` with no database URL is. In the directory the
#: server was started from unless $FILLERAI_HOME says otherwise. Resolved
#: once at import so every endpoint agrees about where it is.
LIBRARY = Store.default()

#: The database, once :func:`serve` has opened one, and the accounts on top
#: of it. Both None means the single-user tool: no login, one file library,
#: exactly what this was before there were accounts.
DATABASE: Database | None = None
AUTH: Auth | None = None

#: Whether the bot reads chat phrases with a language model. Off unless
#: ``serve --bot-llm`` or ``$FILLERAI_BOT_LLM`` turns it on: having a key is
#: not enough, because this sends what end users type, not just field names.
BOT_LLM = False
BOT_LLM_VARIABLE = "FILLERAI_BOT_LLM"

#: Whether the chat's microphone may send recordings here to be transcribed
#: with an OpenAI key. Off unless ``serve --bot-transcribe`` or
#: ``$FILLERAI_BOT_TRANSCRIBE``: it sends a person's voice to a third party.
BOT_TRANSCRIBE = False
#: The port the sample application listens on when ``serve`` started it,
#: for the UI to link to; ``None`` when it isn't running.
SAMPLE_APP_PORT: int | None = None
#: The sample application is also served on this server, under this path,
#: so a host that exposes one port (Railway and the like) can still demo it.
SAMPLE_PATH = "/sample"
#: Whether /sample/ is open to visitors who are not signed in. Off by
#: default: the sample app talks to the bot with an administrator's token.
SAMPLE_PUBLIC = False
SAMPLE_PUBLIC_VARIABLE = "FILLERAI_SAMPLE_PUBLIC"
#: What the sample application's token is called, so a restart replaces it.
SAMPLE_APP_TOKEN = "Sample application"
BOT_TRANSCRIBE_VARIABLE = "FILLERAI_BOT_TRANSCRIBE"

#: API keys typed into the UI. In memory, for this run of the server, per
#: user - see :mod:`.keyring` for why they go no further than that.
KEYRING = Keyring()

#: The header a browser must echo the session's token in. A cookie alone
#: would let any page on the machine POST here with the user's credentials
#: attached; a token another origin cannot read is what stops that. Sent by
#: the UI on every call, which is why it is a header and not a form field.
CSRF_HEADER = "X-FillerAI-Token"

#: Origins allowed to call the ``/v1`` integration API from a browser, or
#: None for "nobody said, so work it out". See :func:`allowed_origins`.
CORS_ALLOW: tuple[str, ...] | None = None

#: Endpoints reachable without signing in. Everything else needs a session
#: whenever accounts are on.
PUBLIC = {"/api/auth/login", "/api/meta", "/api/docs/unlock"}

#: Endpoints a user who must change their password may still call. Anything
#: else would be working in an account somebody else knows the password to.
WHILE_LOCKED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout",
                "/api/meta", "/api/docs/unlock"}


@dataclass
class Context:
    """Who is making the request being handled, and what to reply with.

    Held in a :class:`~contextvars.ContextVar` rather than threaded through
    every endpoint. Twenty-odd handlers already take one argument and mean
    it; growing a second one on all of them to serve the three that care
    about the user would be a worse trade than this. Each request thread
    starts with its own empty context, so there is nothing shared to get
    wrong.
    """

    user: User | None = None
    session: Session | None = None
    #: A cookie to set, or "" to clear one, once the handler returns.
    cookie: str | None = None
    #: The documentation cookie to set, the same way. A separate thing from
    #: the session: the docs are opened by a code, not by an account.
    docs_cookie: str | None = None


_CONTEXT: ContextVar[Context] = ContextVar("fillerai_request")


def context() -> Context:
    try:
        return _CONTEXT.get()
    except LookupError:
        # No request in flight: the CLI and the tests call handlers directly.
        return Context()


def current_user() -> User | None:
    return context().user


def require_user() -> User:
    user = current_user()
    if user is None:
        raise ApiError("sign in first", status=401)
    return user


def library() -> Any:
    """The library this request should read and write.

    One database, one library per person inside it. Without a database there
    is one library on disk and no owner, which is the single-user tool.
    """
    if DATABASE is None:
        return LIBRARY
    user = current_user()
    return DatabaseStore(DATABASE, owner=user.id if user else "")


class ApiError(Exception):
    """A problem worth reporting to the user rather than a stack trace."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ----------------------------------------------------------------------
# request payload helpers
# ----------------------------------------------------------------------


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise ApiError(f"missing {key!r} in request")
    return payload[key]


def _schema_from(payload: dict[str, Any]) -> FormSchema:
    try:
        return FormSchema.from_dict(_require(payload, "schema"))
    except ApiError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise ApiError(f"that schema could not be read: {error}") from error


def _bounded_int(payload: dict[str, Any], key: str, default: int,
                 low: int, high: int) -> int:
    value = payload.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a whole number") from None
    if not low <= value <= high:
        raise ApiError(f"{key} must be between {low} and {high}")
    return value


def _summarise(schema: FormSchema, review_below: float = 0.7) -> dict[str, Any]:
    return {
        "fields": len(schema.fields),
        "screens": len(schema.screens),
        "groups": [g for g in schema.groups() if g],
        "needs_review": [
            {"name": f.name, "semantic_type": f.semantic_type, "confidence": f.confidence}
            for f in schema.fields if f.confidence < review_below
        ],
    }


# ----------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------


def api_meta(_: dict[str, Any]) -> dict[str, Any]:
    """Everything the front end needs to render its controls.

    Answered signed in or not, because the login page needs the version and
    needs to know whether there is anybody to sign in as. Signed out it says
    that and stops - the algorithm list is not secret, but an endpoint that
    answers the same either way is one fewer thing to reason about.
    """
    user = current_user()
    base: dict[str, Any] = {
        "version": __version__,
        "accounts": AUTH is not None,
        "signed_in": user is not None,
    }
    if AUTH is not None and user is None:
        return base
    if user is not None:
        base["user"] = user.to_dict()
        base["csrf"] = context().session.csrf if context().session else ""
    base.update({
        "semantic_types": list(SEMANTIC_TYPES),
        "examples": _list_examples(),
        "algorithms": [a.to_dict() for a in algos.all_algorithms()],
        "default_algorithm": algos.DEFAULT,
        "library": str(library().root),
        "bot_llm": BOT_LLM,
        "bot_transcribe": BOT_TRANSCRIBE,
        "sample_app_port": SAMPLE_APP_PORT,
        "sample_app_path": f"{SAMPLE_PATH}/" if SAMPLE_APP_PORT else None,
    })
    return base


def _list_examples() -> list[dict[str, str]]:
    if not EXAMPLES_DIR.is_dir():
        return []
    entries = []
    for path in sorted(EXAMPLES_DIR.iterdir()):
        if path.suffix.lower() in (".html", ".htm"):
            kind, label = "html", "HTML form"
        elif path.name.endswith(".fields.json"):
            kind, label = "spec", "Field spec"
        else:
            continue
        entries.append({"id": path.name, "kind": kind, "label": label,
                        "name": path.stem.replace(".fields", "")})
    return entries


def api_example(payload: dict[str, Any]) -> dict[str, Any]:
    """Read one bundled example, by name only - never an arbitrary path."""
    name = str(_require(payload, "id"))
    known = {entry["id"] for entry in _list_examples()}
    if name not in known:
        raise ApiError(f"no bundled example called {name!r}", status=404)
    path = EXAMPLES_DIR / name
    return {"id": name, "content": path.read_text(encoding="utf-8")}


def api_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn pasted markup or a field spec into an inferred schema."""
    kind = str(payload.get("kind", "html")).lower()
    content = str(_require(payload, "content"))
    name = str(payload.get("name") or "form")

    if not content.strip():
        raise ApiError("there is nothing to extract from yet")

    if kind == "html":
        schema = infer(html_form.extract(content, name=name, source={"kind": "html"}))
    elif kind == "spec":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise ApiError(f"that is not valid JSON: {error}") from error
        # A saved schema round-trips; a field spec gets loaded and inferred.
        try:
            if "schema_version" in data:
                schema = FormSchema.from_dict(data)
            else:
                schema = infer(spec_loader.load(data, source={"kind": "spec"}))
        except (ValueError, KeyError, TypeError) as error:
            raise ApiError(str(error)) from error
    else:
        raise ApiError(f"unknown source kind {kind!r}")

    if not schema.fields:
        raise ApiError("no form fields were found in that source")

    out = {"schema": schema.to_dict(), "summary": _summarise(schema)}
    if payload.get("save", True):
        # The source is kept beside the schema rather than only the schema.
        # Six weeks later the question is "which page did this come off?",
        # and a schema that cannot answer it is half a record.
        source = library().save_source(content, kind=kind, name=name)
        out["source_id"] = source.id
        out["schema_id"] = library().save_schema(schema, parent=source.id).id
    return out


def api_reinfer(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-run inference over an edited schema.

    Fields the user has set by hand are left alone: the editor marks them
    with a confidence of 1.0, which inference treats as authoritative.
    """
    schema = _schema_from(payload)
    return {"schema": infer(schema).to_dict(), "summary": _summarise(schema)}


def api_generate(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate records, and check them unless asked not to."""
    schema = _schema_from(payload)
    count = _bounded_int(payload, "count", 20, 1, MAX_RECORDS)
    seed = payload.get("seed")
    if seed in ("", None):
        seed = None
    else:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ApiError("seed must be a whole number, or empty for random") from None

    try:
        blank_rate = float(payload.get("blank_rate", 0.12))
    except (TypeError, ValueError):
        raise ApiError("blank rate must be a number between 0 and 1") from None
    if not 0.0 <= blank_rate <= 1.0:
        raise ApiError("blank rate must be between 0 and 1")

    dataset = generate(schema, Options(
        count=count,
        seed=seed,
        blank_rate=blank_rate,
        safe_identifiers=bool(payload.get("safe_identifiers", True)),
    ))

    problems: list[str] = []
    if payload.get("check", True):
        problems = validate(schema, dataset.records) + coherence_report(
            schema, dataset.records
        )

    out = {
        "columns": [f.name for f in schema.fields if not f.constraints.read_only],
        "records": dataset.records,
        "problems": problems,
        "count": len(dataset.records),
    }
    if payload.get("save", True):
        parent = _parent_schema(payload, schema)
        out["schema_id"] = parent
        out["dataset_id"] = library().save_dataset(
            dataset.records, parent=parent,
            name=f"{schema.name or 'form'}: {len(dataset.records)} records",
            meta={"seed": seed, "blank_rate": round(blank_rate, 3),
                  "problems": len(problems)},
        ).id
    return out


def api_export(payload: dict[str, Any]) -> dict[str, Any]:
    """Render already-generated records in the requested format."""
    schema = _schema_from(payload)
    records = _require(payload, "records")
    if not isinstance(records, list):
        raise ApiError("records must be a list")
    fmt = str(payload.get("format", "json")).lower()
    try:
        text = Dataset(schema=schema, records=records).render(fmt)
    except ValueError as error:
        raise ApiError(str(error)) from error
    return {"format": fmt, "text": text,
            "filename": f"{schema.name or 'form'}.{fmt if fmt != 'ndjson' else 'ndjson'}"}



def _parent_schema(payload: dict[str, Any], schema: FormSchema) -> str:
    """The library schema these records belong to, stored now if need be.

    The UI sends back the id it was given when the schema was extracted, so
    the usual case is a lookup. A schema edited in the browser, or one from a
    session that started before the library existed, is stored here instead -
    an entry with no parent is a worse record than a duplicate schema.
    """
    given = payload.get("schema_id")
    if given and library().has(str(given)):
        return str(given)
    return library().save_schema(schema).id


# ----------------------------------------------------------------------
# real records
# ----------------------------------------------------------------------
#
# The browser keeps the uploaded file and sends it with each request, so
# nothing about an upload is held here between calls: reading, previewing a
# clean and saving the result are three questions about the same text. The
# file is the user's own data and lives in this process only for as long as
# one request takes; what is kept is the cleaned dataset, and only when they
# say save.


def _table_from(payload: dict[str, Any]) -> realdata.Table:
    text = _require(payload, "text")
    if not isinstance(text, str):
        raise ApiError("text must be the file's contents")
    try:
        return realdata.read_table(text, str(payload.get("filename") or ""))
    except realdata.DataError as error:
        raise ApiError(str(error)) from error


def _fixes_from(payload: dict[str, Any]) -> frozenset[str]:
    given = payload.get("fixes")
    if given is None:
        return realdata.DEFAULT_FIXES
    if not isinstance(given, list):
        raise ApiError("fixes must be a list of fix names")
    unknown = [str(g) for g in given if str(g) not in realdata.FIX_NAMES]
    if unknown:
        raise ApiError(f"there is no fix called {unknown[0]!r}")
    return frozenset(str(g) for g in given)


def api_data_read(payload: dict[str, Any]) -> dict[str, Any]:
    """What is in an uploaded file, and which field each column looks like."""
    table = _table_from(payload)
    out: dict[str, Any] = {
        "format": table.format,
        "columns": table.columns,
        "rows": len(table.rows),
        "preview": table.preview(),
        "fixes": [{"key": key, "label": label, "on": key in realdata.DEFAULT_FIXES}
                  for key, label in realdata.FIXES],
    }
    if payload.get("schema"):
        schema = _schema_from(payload)
        out["mapping"] = realdata.suggest_mapping(schema, table.columns)
        out["fields"] = [{"name": f.name, "label": f.label or f.name}
                         for f in realdata.fillable(schema)]
    return out


def api_data_clean(payload: dict[str, Any]) -> dict[str, Any]:
    """Clean an uploaded file against the form, and keep it if asked.

    Without ``save`` this is the preview: the report and the first rows, so
    the fixes can be switched on and off and looked at before anything is
    stored. With it, the records go into the library as a dataset under the
    form's schema - marked as real, with the file they came from and what
    cleaning did - and come back whole, ready for Train or for testing a
    model. The cap is the one Train has, said out loud when it bites.
    """
    schema = _schema_from(payload)
    table = _table_from(payload)
    mapping = payload.get("mapping")
    if mapping is not None and not isinstance(mapping, dict):
        raise ApiError("mapping must be an object of column names to field names")
    try:
        cleaned = realdata.clean(schema, table, mapping, _fixes_from(payload),
                                 limit=MAX_RECORDS)
    except realdata.DataError as error:
        raise ApiError(str(error)) from error

    report = cleaned.report()
    save = bool(payload.get("save"))
    out: dict[str, Any] = {
        "report": report,
        "columns": [f.name for f in realdata.fillable(schema)],
        "count": cleaned.rows_out,
        "records": cleaned.records if save else cleaned.records[:PREVIEW_ROWS],
        "max_records": MAX_RECORDS,
    }
    if save:
        if not cleaned.records:
            raise ApiError("cleaning left no rows to keep")
        parent = _parent_schema(payload, schema)
        filename = str(payload.get("filename") or "upload")[:120]
        fixed = sum(f["count"] for f in report["fixes"]
                    if f["on"] and f["unit"] == "cells")
        out["schema_id"] = parent
        out["dataset_id"] = library().save_dataset(
            cleaned.records, parent=parent,
            name=f"{schema.name or 'form'}: {cleaned.rows_out} real records",
            meta={"origin": "real", "file": filename,
                  "rows": f"{cleaned.rows_out} of {report['rows_in']}",
                  "fixed": fixed, "rejected": sum(p["count"] for p in report["problems"])},
        ).id
    return out


def api_data_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """A form read off a file's columns, for records that arrive without one.

    What comes back is what ``/api/extract`` returns, because it is the same
    thing: a field spec, read and inferred like any other, kept in the
    library as a spec source with its schema under it. The mapping that goes
    with it is trivially every column to its own field.
    """
    table = _table_from(payload)
    name = str(payload.get("name") or "form")
    spec = realdata.spec_from_table(table, name)
    schema = infer(spec_loader.load(spec, source={"kind": "spec", "from": "data"}))
    content = json.dumps(spec, indent=2, ensure_ascii=False)
    out = {
        "schema": schema.to_dict(),
        "summary": _summarise(schema),
        "spec": content,
        "mapping": realdata.suggest_mapping(schema, table.columns),
    }
    if payload.get("save", True):
        source = library().save_source(content, kind="spec", name=name)
        out["source_id"] = source.id
        out["schema_id"] = library().save_schema(schema, parent=source.id).id
    return out


# Records a test scores. Every one is a full form filled twice over - once
# to score the fields, once to cost the time - so this is where the wait is.
MAX_TEST_RECORDS = 1000


def api_evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    """Test a model on records it was not trained on - real ones, usually.

    Two measurements of the same thing, because each answers a question the
    other cannot. :func:`evaluate` says, field by field, how often the model
    was right and whether its confidence meant anything; the sweep says what
    that was worth in an agent's time. Both fill each record from its own
    seed fields, which is the honest test: the model sees only what an agent
    would have typed.

    A test on the very records a model learned from is allowed and flagged,
    because it measures memory rather than the form.
    """
    token = str(_require(payload, "model_id"))
    model = _recall(token)
    dataset_id = str(payload.get("dataset_id") or "")
    if dataset_id:
        if not library().has(dataset_id):
            raise ApiError(f"there is nothing in the library called {dataset_id}",
                           status=404)
        try:
            records = library().load_records(dataset_id)
        except StoreError as error:
            raise ApiError(str(error), status=404) from error
    else:
        records = _records_from(payload)
    if not records:
        raise ApiError("there are no records to test on")
    total = len(records)
    records = records[:MAX_TEST_RECORDS]

    present = [name for name in model.targets()
               if any(features.normalise(r.get(name)) for r in records)]
    if not present:
        raise ApiError("none of these records' fields are on this model's form - "
                       "were they cleaned against another form?")

    given = payload.get("seeds")
    if isinstance(given, list) and given:
        seeds = [str(s) for s in given if str(s) in model.profiles]
    else:
        seeds = suggest_seed_fields(model, _bounded_int(payload, "ask", 3, 1, 8))
    threshold = _threshold_from(payload)

    report = evaluate(model, records, seeds=seeds, threshold=threshold).to_dict()
    swept = simulate_many(model, records, seeds, threshold=threshold).to_dict()

    entry_id = _MODEL_ENTRIES.get(token)
    learned_from = bool(dataset_id and entry_id and any(
        e.id == dataset_id for e in library().lineage(entry_id)))
    out: dict[str, Any] = {
        "evaluation": report,
        "sweep": swept,
        "seeds": seeds,
        "records": len(records),
        "total": total,
        "fields_present": len(present),
        "fields_total": len(model.targets()),
        "learned_from": learned_from,
    }
    if dataset_id:
        entry = library().get(dataset_id)
        out["dataset"] = entry.to_dict()
    return out


# ----------------------------------------------------------------------
# training
# ----------------------------------------------------------------------

_MODELS: "OrderedDict[str, AutofillModel]" = OrderedDict()
#: Which library entry each cached model came out of, where it has one. A
#: model trained with saving turned off has none, and the stages that show
#: where a model came from say so rather than inventing a chain.
_MODEL_ENTRIES: dict[str, str] = {}


def _remember(model: AutofillModel, entry_id: str | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    _MODELS[token] = model
    if entry_id:
        _MODEL_ENTRIES[token] = entry_id
    while len(_MODELS) > MAX_CACHED_MODELS:
        evicted, _ = _MODELS.popitem(last=False)
        _MODEL_ENTRIES.pop(evicted, None)
    return token


def _saved_as(token: str, entry_id: str) -> None:
    """Note the library entry a cached model was stored as.

    A training run hands the model to the cache before it saves it - the
    handle goes out with the result whether it is saved or not - so the link
    back to the library is made here rather than at :func:`_remember`.
    """
    if token in _MODELS:
        _MODEL_ENTRIES[token] = entry_id


def _recall(token: str) -> AutofillModel:
    model = _MODELS.get(token)
    if model is None:
        raise ApiError(
            "that model is no longer loaded - train it again to carry on",
            status=404,
        )
    # Touch it, so the model in use is not the one evicted next.
    _MODELS.move_to_end(token)
    return model


def _records_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = _require(payload, "records")
    if not isinstance(records, list) or not records:
        raise ApiError("training needs a list of records - generate some first")
    if len(records) > MAX_RECORDS:
        raise ApiError(f"that is more than {MAX_RECORDS} records for the UI to train on")
    if not all(isinstance(r, dict) for r in records):
        raise ApiError("every record must be an object of field names to values")
    return records


def _options_from(payload: dict[str, Any]) -> TrainOptions:
    """The training options a request is asking for, checked."""
    name = str(payload.get("algorithm") or algos.DEFAULT)
    try:
        algorithm = algos.get(name)
    except ValueError as error:
        raise ApiError(str(error)) from error

    seed = payload.get("seed")
    try:
        seed = None if seed in ("", None) else int(seed)
    except (TypeError, ValueError):
        raise ApiError("seed must be a whole number, or empty for random") from None

    # Only the knobs this algorithm declares, and only inside the range it
    # declared them in. The browser sends whatever is on screen, and a
    # forest with a depth of 900 is a hang rather than a model.
    tuning: dict[str, Any] = {}
    given = payload.get("tuning") or {}
    if not isinstance(given, dict):
        raise ApiError("tuning must be an object of settings")
    for key, _label, kind, default, low, high in algorithm.knobs:
        if key not in given or given[key] in ("", None):
            continue
        try:
            value = int(given[key]) if kind == "int" else float(given[key])
        except (TypeError, ValueError):
            raise ApiError(f"{key} must be a number") from None
        if not low <= value <= high:
            raise ApiError(f"{key} must be between {low} and {high}")
        tuning[key] = value

    return TrainOptions(
        algorithm=algorithm.name,
        holdout=0.25,
        seed=seed,
        tuning=tuning,
        use_rules=bool(payload.get("use_rules", True)),
    )


def _train_result(model: AutofillModel, records: list[dict[str, Any]],
                  holdout: list[dict[str, Any]], payload: dict[str, Any],
                  options: TrainOptions) -> dict[str, Any]:
    """Everything the Train panel draws, from a model that is already fitted."""
    ask = _bounded_int(payload, "ask", 3, 1, 8)
    given = payload.get("seeds")
    if isinstance(given, list) and given:
        seeds = [str(s) for s in given if str(s) in model.profiles]
    else:
        seeds = suggest_seed_fields(model, ask)

    report = model.field_report()
    result: dict[str, Any] = {
        "model_id": _remember(model),
        "algorithm": model.algorithm,
        "seeds": seeds,
        "fields": report,
        "trained_on": model.trained_on,
        "held_out": model.held_out,
        "threshold": ACCEPT_ABOVE,
        "engine": model.engine.summary(),
        # How loudly each voter was told to speak. This one goes out as the
        # structured form rather than ``summary()``: the panel draws a weight
        # per feature as a signed bar and needs the numbers apart from the
        # words, and it is the browser's business what to call each feature.
        # A combiner that was never measured carries no votes, and the panel
        # is then simply not there rather than there and empty.
        "combiner": model.combiner.to_dict(),
        "settings": model.settings,
        "rules": [
            {"target": d.target, "inputs": d.inputs, "kind": d.kind,
             "accuracy": round(d.accuracy, 4)}
            for d in model.derivations.values()
        ],
        "counts": {
            "predictable": sum(1 for r in report if r["how"] != "you"),
            "yours": sum(1 for r in report if r["how"] == "you"),
            "total": len(report),
        },
        # Fields the chosen engine can draw as a tree, for the panel that
        # shows one. Empty for the engines that have no tree to draw.
        "drawable": sorted(getattr(model.engine, "trees", {})),
    }
    if holdout:
        result["evaluation"] = evaluate(model, holdout, seeds=seeds).to_dict()

    if payload.get("save", True):
        parent = payload.get("dataset_id")
        if not parent or not library().has(str(parent)):
            schema_id = _parent_schema(payload, model.schema)
            parent = library().save_dataset(
                records, parent=schema_id,
                name=f"{model.schema.name or 'form'}: {len(records)} records").id
        evaluation = result.get("evaluation") or {}
        # Written as they read. The library's metadata is display data - what
        # a row in a list should say - and a share stored as 1.0 comes out of
        # a listing as "right 1", which is not what anybody meant.
        scores = {
            key: f"{evaluation[source] * 100:.0f}%"
            for key, source in (("fills", "coverage"), ("right", "accepted_accuracy"))
            if evaluation.get(source) is not None
        }
        model_entry = library().save_model(
            model, parent=str(parent),
            name=f"{model.schema.name or 'form'}: {options.algorithm}",
            meta={"seeds": seeds, **scores},
        )
        result["model_entry_id"] = model_entry.id
        result["dataset_id"] = str(parent)
        # So a later stage can say which entry this model is, and walk back up
        # to the records and the page behind it.
        _saved_as(result["model_id"], model_entry.id)
        # The script this run is, kept under the model it produced. It is
        # generated from the options, so it looks redundant right up until a
        # default changes: then this is what that model was trained by, and
        # what today's code would write is not.
        result["script_entry_id"] = library().save_script(
            script_writer.script(options, model.schema, seeds=seeds),
            parent=model_entry.id,
            name=f"{model.schema.name or 'form'}: {options.algorithm} script",
            meta={"algorithm": options.algorithm},
        ).id
    return result


def api_train(payload: dict[str, Any]) -> dict[str, Any]:
    """Learn an autofill model, and say how well it did on unseen records.

    The synchronous one, kept because the CLI and the tests want a call that
    returns a model rather than a handle. The UI uses ``/api/train/start``,
    which is the same work on a worker thread with the log readable while it
    runs.
    """
    schema = _schema_from(payload)
    records = _records_from(payload)
    options = _options_from(payload)
    # The split is deterministic given the options, so calling it again here
    # recovers exactly the rows the fit held back - the score below is
    # measured on records the model has not seen.
    _, holdout = split_records(records, options)
    model = train(schema, records, options)
    return _train_result(model, records, holdout, payload, options)


# ----------------------------------------------------------------------
# training, watched
# ----------------------------------------------------------------------


class Run:
    """One training run on a worker thread, and its log.

    The UI polls rather than holding a stream open. Polling is the duller
    choice and the right one here: a fit is seconds rather than minutes, a
    poll that arrives late costs nothing, and a browser tab that is closed
    mid-run leaves a thread finishing quietly instead of a half-written
    response nobody reads. The trace does the thread-safety; this only
    carries the result.
    """

    def __init__(self, run_id: str, options: TrainOptions) -> None:
        self.id = run_id
        self.options = options
        self.trace = Trace()
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def start(self, schema: FormSchema, records: list[dict[str, Any]],
              payload: dict[str, Any]) -> None:
        # Who asked for this run, read here on the request thread while the
        # context is still in scope. A new thread starts with an empty one,
        # so a worker that did not carry this would save the model it just
        # fitted into the unowned library and the person who trained it
        # would not find it in theirs. Its own Context rather than the
        # caller's object: the worker inherits who the run is for, and
        # cannot write a cookie onto a request that has long since replied.
        caller = context()

        def work() -> None:
            _CONTEXT.set(Context(user=caller.user, session=caller.session))
            try:
                self.options.trace = self.trace
                _, holdout = split_records(records, self.options)
                model = train(schema, records, self.options)
                self.result = _train_result(model, records, holdout, payload,
                                            self.options)
                self.trace.finish()
            except Exception as error:  # noqa: BLE001 - reported, not raised
                # There is nobody to raise to: this is a worker thread, and
                # the request that started it returned long ago. The failure
                # belongs in the log the user is watching.
                self.error = str(error) or error.__class__.__name__
                traceback.print_exc()
                self.trace.finish(f"training failed: {self.error}")

        self.thread = threading.Thread(target=work, name=f"train-{self.id}",
                                       daemon=True)
        self.thread.start()


_RUNS: "OrderedDict[str, Run]" = OrderedDict()


def _run(run_id: str) -> Run:
    run = _RUNS.get(run_id)
    if run is None:
        raise ApiError("that training run is no longer here - start it again",
                       status=404)
    return run


def api_train_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Begin a training run and hand back what it will do, before it does it."""
    schema = _schema_from(payload)
    records = _records_from(payload)
    options = _options_from(payload)

    run = Run(uuid.uuid4().hex[:16], options)
    _RUNS[run.id] = run
    while len(_RUNS) > MAX_KEPT_RUNS:
        _RUNS.popitem(last=False)
    run.start(schema, records, payload)

    algorithm = algos.get(options.algorithm)
    return {
        "run_id": run.id,
        "algorithm": algorithm.to_dict(),
        # The script and the recipe are returned now rather than at the end,
        # because the point of showing them is to say what is about to
        # happen while it is happening.
        "recipe": algorithm.recipe,
        "script": script_writer.script(options, schema, seeds=payload.get("seeds") or None),
        "command": script_writer.command(options),
    }


def api_train_log(payload: dict[str, Any]) -> dict[str, Any]:
    """New log lines since ``cursor``, plus the result once there is one."""
    run = _run(str(_require(payload, "run_id")))
    cursor = max(0, _bounded_int(payload, "cursor", 0, 0, 10 ** 7))
    lines = run.trace.since(cursor)[:MAX_LOG_LINES]
    return {
        "run_id": run.id,
        "lines": [line.to_dict() for line in lines],
        "cursor": cursor + len(lines),
        "progress": run.trace.progress(),
        "result": run.result,
        "error": run.error,
    }


def api_train_script(payload: dict[str, Any]) -> dict[str, Any]:
    """The script for a set of options, without running anything.

    So the picker can show what each algorithm would do before one is chosen,
    which is the whole reason the recipe and the script exist.
    """
    options = _options_from(payload)
    schema = None
    if payload.get("schema"):
        schema = _schema_from(payload)
    algorithm = algos.get(options.algorithm)
    return {
        "algorithm": algorithm.to_dict(),
        "recipe": algorithm.recipe,
        "script": script_writer.script(options, schema),
        "command": script_writer.command(options),
    }


def api_train_tree(payload: dict[str, Any]) -> dict[str, Any]:
    """The tree grown for one field, drawn, when the engine has one."""
    model = _recall(str(_require(payload, "model_id")))
    name = str(_require(payload, "field"))
    drawn = getattr(model.engine, "drawn", None)
    return {
        "field": name,
        "lines": drawn(name) if callable(drawn) else [],
        "algorithm": model.algorithm,
    }


def api_predict(payload: dict[str, Any]) -> dict[str, Any]:
    """Finish a record from the fields the user has typed so far."""
    model = _recall(str(_require(payload, "model_id")))
    observed = payload.get("observed") or {}
    if not isinstance(observed, dict):
        raise ApiError("observed must be an object of field names to values")

    unknown = [k for k in observed if k not in model.profiles]
    if unknown:
        raise ApiError(f"this model has no field called {sorted(unknown)[0]!r}")

    threshold = payload.get("threshold", ACCEPT_ABOVE)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise ApiError("threshold must be a number between 0 and 1") from None
    if not 0.0 <= threshold <= 1.0:
        raise ApiError("threshold must be between 0 and 1")

    predictions = model.predict(observed)
    offered = sum(1 for p in predictions.values()
                  if p.known and p.confidence >= threshold)
    return {
        "given": {k: str(v) for k, v in observed.items()},
        "predictions": [p.to_dict() for p in predictions.values()],
        "offered": offered,
        "threshold": threshold,
    }


# ----------------------------------------------------------------------
# simulation
# ----------------------------------------------------------------------

# A sweep is the honest version of the saving - one form is an anecdote -
# but it is also the one endpoint that does real work per request, so it is
# bounded at something that still answers in well under a second.
MAX_SWEEP_CASES = 200


def _threshold_from(payload: dict[str, Any]) -> float:
    threshold = payload.get("threshold", ACCEPT_ABOVE)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise ApiError("threshold must be a number between 0 and 1") from None
    if not 0.0 <= threshold <= 1.0:
        raise ApiError("threshold must be between 0 and 1")
    return threshold


def _case_seed(payload: dict[str, Any]) -> int:
    """A seed for a fresh case, random unless the caller pinned one.

    Returned to the caller either way, so a run that turned up something
    worth looking at twice can be asked for again.
    """
    seed = payload.get("seed")
    if seed in ("", None):
        return random.randrange(1, 2**31 - 1)
    try:
        return int(seed)
    except (TypeError, ValueError):
        raise ApiError("seed must be a whole number, or empty for random") from None


def _new_cases(model: AutofillModel, count: int, seed: int) -> list[dict[str, Any]]:
    """Records for the model's own form that it has never been shown.

    Not drawn from the training set: a form the model has already learned
    would flatter every number on this stage. Blanks are left in at the same
    rate the training data had them, because a real form arrives partly
    inapplicable and a simulation that fills every box is not the job.
    """
    return generate(model.schema, Options(count=count, seed=seed)).records


def _provenance(token: str, model: AutofillModel) -> dict[str, Any]:
    """Which model a stage is running, and the chain of work behind it.

    The question somebody arrives at the Simulate stage with is *whose model
    is this?*, and a handle like ``9f2c1a...`` is not an answer. What comes
    back is what the model is in itself, plus - when it was saved - the
    library entries it descends from, oldest first: the page it was read out
    of, the schema, the records, the model. That is the same lineage the
    library draws, shown where the model is being used rather than in another
    panel.

    A model that was never saved has no chain, and that is reported as the
    absence it is: ``entry`` is None and ``lineage`` is empty, which is
    honest about a model that exists only until this process stops.
    """
    out: dict[str, Any] = {"model_id": token, **model.provenance()}
    try:
        out["algorithm_label"] = algos.get(model.algorithm).label
    except ValueError:
        # A model fitted by an algorithm this build no longer registers. Its
        # own name is still the truthful thing to show.
        out["algorithm_label"] = model.algorithm

    entry_id = _MODEL_ENTRIES.get(token)
    chain = library().lineage(entry_id) if entry_id else []
    # An entry deleted since the model was loaded leaves the model in hand and
    # its record gone. The chain is then empty rather than partly right.
    if not chain or chain[-1].id != entry_id:
        out["entry"] = None
        out["lineage"] = []
        return out
    out["entry"] = chain[-1].to_dict()
    out["lineage"] = [e.to_dict() for e in chain]
    return out


def api_simulate_form(payload: dict[str, Any]) -> dict[str, Any]:
    """The form to draw, and what the model would like typed into it."""
    token = str(_require(payload, "model_id"))
    model = _recall(token)
    return {
        "layout": form_layout(model.schema).to_dict(),
        "seeds": suggest_seed_fields(model, _bounded_int(payload, "ask", 3, 1, 8)),
        "threshold": ACCEPT_ABOVE,
        "effort": DEFAULT_EFFORT.to_dict(),
        "assumptions": DEFAULT_EFFORT.assumptions(),
        # Which model is about to fill this form in, and what it was made
        # from. A simulation nobody can attribute to a model is a demo.
        "model": _provenance(token, model),
    }


def api_simulate_case(payload: dict[str, Any]) -> dict[str, Any]:
    """A fresh form to work: one record the model has not seen."""
    model = _recall(str(_require(payload, "model_id")))
    seed = _case_seed(payload)
    return {"case": _new_cases(model, 1, seed)[0], "seed": seed}


def api_simulate_fill(payload: dict[str, Any]) -> dict[str, Any]:
    """Finish the form from what has been typed, and cost the result."""
    model = _recall(str(_require(payload, "model_id")))
    typed = payload.get("typed") or {}
    if not isinstance(typed, dict):
        raise ApiError("typed must be an object of field names to values")
    unknown = [k for k in typed if k not in model.profiles]
    if unknown:
        raise ApiError(f"this model has no field called {sorted(unknown)[0]!r}")

    case = payload.get("case")
    if case is not None and not isinstance(case, dict):
        raise ApiError("case must be an object of field names to values")

    return simulate(model, typed, case=case,
                    threshold=_threshold_from(payload)).to_dict()


def api_simulate_sweep(payload: dict[str, Any]) -> dict[str, Any]:
    """The same simulation over many fresh forms, so the saving is not one form."""
    model = _recall(str(_require(payload, "model_id")))
    count = _bounded_int(payload, "count", 50, 1, MAX_SWEEP_CASES)
    seed = _case_seed(payload)
    threshold = _threshold_from(payload)

    given = payload.get("seeds")
    if isinstance(given, list) and given:
        seeds = [str(s) for s in given if str(s) in model.profiles]
    else:
        seeds = suggest_seed_fields(model, 3)

    result = simulate_many(model, _new_cases(model, count, seed), seeds,
                           threshold=threshold)
    out = result.to_dict()
    out["seed"] = seed
    out["assumptions"] = DEFAULT_EFFORT.assumptions()
    return out


def api_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand the trained model over as the JSON file the CLI reads."""
    model = _recall(str(_require(payload, "model_id")))
    return {
        "filename": f"{model.schema.name or 'form'}.model.json",
        "text": model.to_json(),
    }


# ----------------------------------------------------------------------
# the library
# ----------------------------------------------------------------------


def _entry(payload: dict[str, Any]) -> str:
    entry_id = str(_require(payload, "id"))
    if not library().has(entry_id):
        raise ApiError(f"there is nothing in the library called {entry_id}",
                       status=404)
    return entry_id


def api_library(payload: dict[str, Any]) -> dict[str, Any]:
    """Everything in the library, with the lineage drawn out.

    Each entry carries its ancestors and its children, so the panel can walk
    either way without another round trip. That is the going back and forth:
    a model knows the dataset it learned from, and a source knows every model
    that ever descended from it.
    """
    kind = payload.get("kind")
    if kind is not None and kind not in KINDS:
        raise ApiError(f"the library holds no {kind!r}")
    limit = _bounded_int(payload, "limit", 200, 1, 2000)
    entries = library().list(kind=kind, parent=payload.get("under"), limit=limit)
    return {
        "root": str(library().root),
        "totals": library().totals(),
        "bytes": library().size(),
        "entries": [
            dict(entry.to_dict(),
                 lineage=[a.id for a in library().lineage(entry.id)[:-1]],
                 children=[c.id for c in library().children(entry.id)])
            for entry in entries
        ],
    }


def api_library_open(payload: dict[str, Any]) -> dict[str, Any]:
    """One entry's contents, ready for the stage that understands it.

    A schema comes back as a schema, a dataset as its records, a model
    loaded into the cache with an id the Train and Simulate panels already
    know how to use. Opening something from the library therefore lands the
    user in the stage it belongs to, with the rest of the chain filled in
    behind them.
    """
    entry_id = _entry(payload)
    entry = library().get(entry_id)
    lineage = library().lineage(entry_id)
    out: dict[str, Any] = {
        "entry": entry.to_dict(),
        "lineage": [a.to_dict() for a in lineage],
        "children": [c.to_dict() for c in library().children(entry_id)],
    }

    # Whatever is above this entry, so the panel can restore the whole chain
    # and not just the leaf.
    for ancestor in lineage:
        if ancestor.kind == "schema":
            out["schema"] = library().payload(ancestor.id)
            out["schema_id"] = ancestor.id
        elif ancestor.kind == "source":
            payload_body = library().payload(ancestor.id)
            out["source"] = payload_body.get("content", "")
            out["source_kind"] = payload_body.get("kind", "html")
            out["source_id"] = ancestor.id
        elif ancestor.kind == "dataset":
            out["dataset_id"] = ancestor.id

    try:
        if entry.kind == "script":
            out["script"] = library().load_script(entry_id)
        elif entry.kind == "dataset":
            out["records"] = library().load_records(entry_id)
        elif entry.kind == "model":
            model = library().load_model(entry_id)
            out["model_id"] = _remember(model, entry_id)
            out["algorithm"] = model.algorithm
            out["seeds"] = entry.meta.get("seeds") or suggest_seed_fields(model, 3)
            out["fields"] = model.field_report()
            out["drawable"] = sorted(getattr(model.engine, "trees", {}))
            out["engine"] = model.engine.summary()
            out["combiner"] = model.combiner.to_dict()
            out["counts"] = {
                "predictable": sum(1 for r in out["fields"] if r["how"] != "you"),
                "yours": sum(1 for r in out["fields"] if r["how"] == "you"),
                "total": len(out["fields"]),
            }
            out["trained_on"] = model.trained_on
            out["held_out"] = model.held_out
            out["rules"] = [
                {"target": d.target, "inputs": d.inputs, "kind": d.kind,
                 "accuracy": round(d.accuracy, 4)}
                for d in model.derivations.values()
            ]
    except StoreError as error:
        raise ApiError(str(error), status=404) from error
    return out


def api_library_export(payload: dict[str, Any]) -> dict[str, Any]:
    """One entry as the file it would be on disk.

    A script comes out as the Python it is, not as JSON with the Python
    inside a string: the point of keeping it is that it can be run.
    """
    entry_id = _entry(payload)
    entry = library().get(entry_id)
    if entry.kind == "script":
        return {"filename": f"{entry.id}.py",
                "text": library().load_script(entry_id)}
    suffix = {"source": "source", "schema": "schema", "dataset": "records",
              "model": "model"}[entry.kind]
    return {
        "filename": f"{entry.id}.{suffix}.json",
        "text": json.dumps(library().payload(entry_id), indent=2, ensure_ascii=False),
    }


def api_library_delete(payload: dict[str, Any]) -> dict[str, Any]:
    entry_id = _entry(payload)
    try:
        removed = library().delete(entry_id, cascade=bool(payload.get("cascade")))
    except StoreError as error:
        raise ApiError(str(error)) from error
    # The integration API keeps models loaded between calls. A deleted entry
    # that is still being answered for would be the worst kind of stale.
    for gone in removed:
        rest.forget(gone)
    return {"removed": removed}


def api_library_rename(payload: dict[str, Any]) -> dict[str, Any]:
    entry_id = _entry(payload)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError("a name cannot be empty")
    return {"entry": library().rename(entry_id, name[:120]).to_dict()}



# ----------------------------------------------------------------------
# signing in, and who you are
# ----------------------------------------------------------------------


def _auth() -> Auth:
    if AUTH is None:
        raise ApiError("this server is running without accounts", status=404)
    return AUTH


def _as_api(error: AuthError) -> ApiError:
    """An authentication failure, in the shape the UI already handles."""
    return ApiError(error.message, status=error.status)


def api_login(payload: dict[str, Any]) -> dict[str, Any]:
    """Check a username and password, and start a session if they match."""
    auth = _auth()
    try:
        user = auth.authenticate(str(payload.get("username") or ""),
                                 str(payload.get("password") or ""))
    except AuthError as error:
        raise _as_api(error) from error

    session = auth.start_session(user, agent=str(payload.get("agent") or ""))
    holder = context()
    holder.user, holder.session = user, session
    holder.cookie = auth.cookie_value(session)
    return {"user": user.to_dict(), "csrf": session.csrf,
            "must_change": user.must_change}


def api_logout(_: dict[str, Any]) -> dict[str, Any]:
    """End this session here and now, not just in this browser."""
    holder = context()
    if AUTH is not None and holder.session is not None:
        AUTH.end_session(holder.session.id)
    holder.user, holder.session = None, None
    holder.cookie = ""
    return {"signed_out": True}


def api_me(_: dict[str, Any]) -> dict[str, Any]:
    user = require_user()
    session = context().session
    return {
        "user": user.to_dict(),
        "csrf": session.csrf if session else "",
        "admin": user.is_admin,
        "must_change": user.must_change,
    }


def api_change_password(payload: dict[str, Any]) -> dict[str, Any]:
    """Change your own password, having proved you know the current one.

    Setting a password ends every session that account had, which is the
    point when the reason for changing it is that somebody else knew the old
    one. That would include this one, so a fresh session is issued here -
    changing your password should not sign you out of the tab you did it in.
    """
    auth = _auth()
    user = require_user()
    current = str(payload.get("current") or "")
    fresh = str(payload.get("new") or "")
    try:
        auth.authenticate(user.username, current)
    except AuthError:
        raise ApiError("that is not your current password", status=403) from None
    if fresh == current:
        raise ApiError("the new password is the one you already have")
    try:
        auth.set_password(user.id, fresh)
    except AuthError as error:
        raise _as_api(error) from error

    user = auth.get(user.id)
    session = auth.start_session(user)
    holder = context()
    holder.user, holder.session = user, session
    holder.cookie = auth.cookie_value(session)
    return {"user": user.to_dict(), "csrf": session.csrf, "changed": True}


# ----------------------------------------------------------------------
# the admin module
# ----------------------------------------------------------------------


def _target(payload: dict[str, Any]) -> User:
    auth = _auth()
    try:
        return auth.get(str(_require(payload, "id")))
    except AuthError as error:
        raise _as_api(error) from error


def api_users(_: dict[str, Any]) -> dict[str, Any]:
    """Every account, with what each one has in the library.

    The entry counts are here because the question an administrator asks
    before turning somebody off is what would go with them.
    """
    auth = _auth()
    counts = DatabaseStore(DATABASE).owners() if DATABASE is not None else {}
    users = []
    for user in auth.users():
        users.append(dict(user.to_dict(),
                          entries=counts.get(user.id, 0),
                          sessions=auth.sessions_for(user.id)))
    return {
        "users": users,
        "roles": list(ROLES),
        "admins": auth.admins(),
        "you": require_user().id,
    }


def api_user_create(payload: dict[str, Any]) -> dict[str, Any]:
    """Make an account, generating a password when none was given.

    A generated password is returned once, here, and never stored in a form
    anybody can read it back out of - the administrator hands it over and it
    has to be changed on first use.
    """
    auth = _auth()
    password = str(payload.get("password") or "")
    generated = None
    if not password:
        password = generated = suggest_password()
    try:
        user = auth.create_user(
            str(payload.get("username") or ""), password,
            role=str(payload.get("role") or "user"),
            display_name=str(payload.get("display_name") or ""),
            must_change=bool(generated) or bool(payload.get("must_change")),
        )
    except AuthError as error:
        raise _as_api(error) from error
    return {"user": user.to_dict(), "password": generated}


def api_user_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Change a role, a display name, or whether an account works at all."""
    auth = _auth()
    user = _target(payload)
    try:
        if "display_name" in payload:
            user = auth.set_display_name(user.id, str(payload["display_name"]))
        if "role" in payload and str(payload["role"]) != user.role:
            user = auth.set_role(user.id, str(payload["role"]))
        if "active" in payload and bool(payload["active"]) != user.active:
            user = auth.set_active(user.id, bool(payload["active"]))
    except AuthError as error:
        raise _as_api(error) from error
    return {"user": user.to_dict()}


def api_user_password(payload: dict[str, Any]) -> dict[str, Any]:
    """Reset somebody else's password, to something they must then change."""
    auth = _auth()
    user = _target(payload)
    password = str(payload.get("password") or "")
    generated = None
    if not password:
        password = generated = suggest_password()
    try:
        auth.set_password(user.id, password, must_change=True)
    except AuthError as error:
        raise _as_api(error) from error
    return {"user": user.to_dict(), "password": generated}


def api_user_delete(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove an account and everything in its library.

    The library goes because it is theirs and nobody else can reach it. The
    UI says how much that is before it asks, which is what ``entries`` on the
    listing is for.
    """
    auth = _auth()
    user = _target(payload)
    if user.id == require_user().id:
        raise ApiError("you cannot delete the account you are signed in as")
    try:
        auth.delete_user(user.id)
    except AuthError as error:
        raise _as_api(error) from error
    return {"removed": user.id, "username": user.username}


def api_admin_database(_: dict[str, Any]) -> dict[str, Any]:
    """Where the data actually is, and how much of it there is."""
    if DATABASE is None:
        raise ApiError("this server is running without a database", status=404)
    shared = DatabaseStore(DATABASE)
    return {
        "database": DATABASE.describe(),
        "entries": sum(shared.owners().values()),
        "file_library": str(LIBRARY.root),
        "file_library_exists": Path(str(LIBRARY.root)).is_dir(),
    }


def api_admin_import(_: dict[str, Any]) -> dict[str, Any]:
    """Copy the file library on disk into the signed-in user's library."""
    if DATABASE is None:
        raise ApiError("this server is running without a database", status=404)
    result = import_store(LIBRARY, library())
    return {"copied": len(result["copied"]), "skipped": len(result["skipped"]),
            "failed": result["failed"], "from": result["from"]}


# ----------------------------------------------------------------------
# the optional language-model features
# ----------------------------------------------------------------------
#
# Every handler below imports ``..llm`` *inside* the function. That is the
# fence described in ``fillerai/llm/__init__.py`` and enforced by
# ``tests/test_llm_fence.py``: one module-scope import here would make a
# network-capable package mandatory for every command that serves a page.


def _who() -> str:
    """The keyring row this request belongs to."""
    user = current_user()
    return user.id if user else SINGLE_USER


def _llm_settings(task: str, *, provider: str | None = None,
                  model: str | None = None, who: str | None = None):
    """What this user's next call would be made with.

    A key typed into the UI is laid over the environment as that provider's
    own variable, so the inference in :func:`fillerai.llm.config.
    choose_provider` decides exactly as it does at a terminal - one rule about
    which service is meant, not two.
    """
    from ..llm import providers as llm_providers
    from ..llm.config import Settings

    who = who if who is not None else _who()
    chosen = KEYRING.preference(who)
    environ = _llm_environ(who)
    try:
        return Settings.resolve(
            task,
            provider=provider or chosen.provider or None,
            model=model or chosen.model or None,
            environ=environ,
        )
    except ValueError as error:
        raise ApiError(str(error)) from None


def _llm_environ(who: str) -> dict[str, str]:
    """The environment with this user's typed keys laid over it."""
    from ..llm import providers as llm_providers

    environ = dict(os.environ)
    for name, key in KEYRING.keys(who).items():
        try:
            environ[llm_providers.get(name).key_variable] = key
        except ValueError:
            continue
    return environ


def _llm_status() -> dict[str, Any]:
    from ..llm import providers as llm_providers
    from ..llm import transport as llm_transport
    from ..llm.config import KEY_VARIABLE

    who = _who()
    typed = KEYRING.typed(who)
    settings = _llm_settings("rules")
    sdk = llm_transport.sdk_for(settings)

    # The inference reports the environment variable it read, which is the
    # right answer at a terminal and the wrong one here: a key typed into the
    # box is laid over that variable, so the unedited reason would tell
    # somebody their shell did something they did it themselves.
    reason = settings.provider_reason
    if settings.provider in typed:
        if reason == f"${settings.api.key_variable} is set":
            reason = "it is the only service you have saved a key for"
        elif reason.startswith("the default;"):
            reason = "you have saved more than one key and have not said which to use"

    return {
        "providers": [
            {
                "name": p.name,
                "label": p.label,
                "key_variable": p.key_variable,
                "models": dict(p.task_models),
                # Where this provider's key is coming from, if anywhere. The
                # key itself is never in this payload in any form.
                "source": ("typed here" if p.name in typed
                           else "environment"
                           if os.environ.get(p.key_variable) or os.environ.get(KEY_VARIABLE)
                           else ""),
                "typed": p.name in typed,
            }
            for p in llm_providers.PROVIDERS.values()
        ],
        "provider": settings.provider,
        "label": settings.api.label,
        "reason": reason,
        "models": {task: _llm_settings(task).model for task in ("rules", "typing", "chat")},
        "bot_llm": BOT_LLM,
        "key": settings.redacted(),
        "configured": settings.configured,
        "transport": sdk.label if sdk else llm_transport.UrllibTransport.label,
        "endpoint": settings.endpoint,
        "custom_base_url": settings.custom_base_url,
        "preference": KEYRING.preference(who).to_dict(),
        "key_variable": KEY_VARIABLE,
    }


def api_llm_status(_: dict[str, Any]) -> dict[str, Any]:
    """Which service the next call would go to, and what decided that."""
    return _llm_status()


def api_llm_key(payload: dict[str, Any]) -> dict[str, Any]:
    """Remember an API key for this user, in memory, until the server stops.

    An empty key forgets one. Nothing about the key comes back in the reply
    beyond the four characters :meth:`Settings.redacted` shows.
    """
    from ..llm import providers as llm_providers

    name = str(_require(payload, "provider")).strip().lower()
    try:
        provider = llm_providers.get(name)
    except ValueError as error:
        raise ApiError(str(error)) from None

    key = str(payload.get("key") or "")
    if len(key) > MAX_KEY_CHARS:
        raise ApiError("that does not look like an API key - it is too long")
    KEYRING.set_key(_who(), provider.name, key)
    return _llm_status()


def api_llm_preference(payload: dict[str, Any]) -> dict[str, Any]:
    """Which service and model this user wants, when more than one would do."""
    from ..llm import providers as llm_providers

    provider = payload.get("provider")
    if provider is not None:
        provider = str(provider).strip().lower()
        if provider and provider not in llm_providers.PROVIDERS:
            raise ApiError(f"no language-model provider called {provider!r}")
    model = payload.get("model")
    if model is not None:
        model = str(model).strip()
        if len(model) > MAX_MODEL_CHARS:
            raise ApiError("that is not a model name")
    KEYRING.set_preference(_who(), provider=provider, model=model)
    return _llm_status()


def api_llm_forget(_: dict[str, Any]) -> dict[str, Any]:
    """Drop every key this user typed, without touching the environment."""
    KEYRING.forget(_who())
    return _llm_status()


def _proposal_payload(entry: dict[str, Any]):
    from ..llm.rules import Proposal

    try:
        return Proposal.from_payload(entry)
    except (ValueError, KeyError, TypeError) as error:
        raise ApiError(f"that rule could not be read: {error}") from None


def api_llm_rules_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """What asking would cost, before anything is sent."""
    from ..llm import rules as llm_rules

    schema = _schema_from(payload)
    settings = _llm_settings("rules")
    estimate = llm_rules.estimate(schema, settings)
    groups = llm_rules.batches(schema)
    return {
        "calls": len(groups),
        "per_call": len(groups[0]),
        "provider": settings.provider,
        "label": settings.api.label,
        "model": settings.model,
        "configured": settings.configured,
        "input_tokens": estimate.input_tokens,
        "output_tokens": estimate.output_tokens,
        "dollars": round(estimate.dollars, 4),
        "priced": estimate.priced,
        "lines": estimate.describe() + ([
            f"too large for one answer: {len(groups)} calls, "
            f"{len(groups[0])} fields at a time"
        ] if len(groups) > 1 else []),
    }


def api_llm_rules_propose(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask for this form's rules, and run the answer through the gate.

    Synchronous on purpose. It is one call and a couple of generated samples,
    and doing it on a worker thread would mean carrying the request's user
    across to it - the bug that hid every trained model from its owner once
    already.
    """
    from ..llm.client import ReplyError
    from ..llm.config import ConfigError
    from ..llm.cost import SpendRefused
    from ..llm.transport import TransportError
    from ..llm import rules as llm_rules

    schema = _schema_from(payload)
    settings = _llm_settings("rules")
    if not settings.configured:
        raise ApiError(
            f"no API key for {settings.api.label} yet - add one under Your account"
        )
    sample = _bounded_int(payload, "sample", llm_rules.CHECK_SAMPLE, 20, MAX_RECORDS)
    try:
        spend = float(payload.get("max_spend", DEFAULT_MAX_SPEND))
    except (TypeError, ValueError):
        raise ApiError("max_spend must be a number") from None

    try:
        proposals = llm_rules.propose(schema, settings=settings,
                                      sample=sample, max_spend=spend)
    except (ConfigError, SpendRefused, TransportError, ReplyError, ValueError) as error:
        raise ApiError(str(error)) from None

    out = proposals.to_dict()
    out["provider"] = settings.provider
    out["label"] = settings.api.label
    return out


def api_llm_rules_apply(payload: dict[str, Any]) -> dict[str, Any]:
    """Declare the chosen rules on the schema, and keep the result.

    The rules are re-read through the same parser that checked them, so a
    browser cannot smuggle one past the gate by editing the list on its way
    back.
    """
    from ..llm import rules as llm_rules

    schema = _schema_from(payload)
    chosen = _require(payload, "rules")
    if not isinstance(chosen, list) or not chosen:
        raise ApiError("choose at least one rule to apply")

    proposals = [_proposal_payload(entry) for entry in chosen]
    verdicts = llm_rules.check(schema, proposals, sample=llm_rules.CHECK_SAMPLE)
    refused = [f"{v.proposal.field}: {v.problem}" for v in verdicts if not v.kept]
    if refused:
        raise ApiError("those rules do not check out: " + "; ".join(refused))

    ruled = llm_rules.with_rules(schema, proposals)
    out = {"schema": ruled.to_dict(), "summary": _summarise(ruled),
           "applied": [p.field for p in proposals]}
    if payload.get("save", True):
        entry = library().save_schema(ruled, parent=payload.get("parent") or None)
        out["schema_id"] = entry.id
    return out


# ----------------------------------------------------------------------
# API tokens, and the integration surface they open
# ----------------------------------------------------------------------


def tokens() -> Tokens:
    """The token store, or the reason there isn't one.

    A token names an account whose library it reads, so without accounts
    there is nothing for one to be. That is not a gap: running with
    ``--no-auth`` is the single-user tool on loopback, where ``/v1`` answers
    without a credential because everything else already does.
    """
    if AUTH is None or DATABASE is None:
        raise ApiError(
            "API tokens belong to an account, and this server is running "
            "without them - the /v1 service answers this machine without a "
            "token. Start it with accounts on to issue one.", status=409)
    return Tokens(DATABASE)


def api_tokens(_: dict[str, Any]) -> dict[str, Any]:
    """Every token this account has issued. Never a secret: there are none
    stored to return."""
    store = tokens()
    user = require_user()
    return {"tokens": [t.to_dict() for t in store.list(user.id)],
            "api": rest.VERSION,
            "cors": list(allowed_origins())}


def api_token_create(payload: dict[str, Any]) -> dict[str, Any]:
    """Issue one, and hand back the secret this once.

    The reply is the only time the string exists outside the caller's
    machine, which is said here as well as in the UI, because somebody will
    close the panel before copying it.
    """
    store = tokens()
    user = require_user()
    name = str(payload.get("name") or "").strip()[:MAX_TOKEN_NAME]

    days = payload.get("days")
    if days in ("", None):
        days = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ApiError("an expiry is a number of days") from None
        if not 1 <= days <= 3650:
            raise ApiError("an expiry is between 1 and 3650 days")

    model_id = str(payload.get("model_id") or "").strip() or None
    if model_id and not library().has(model_id):
        raise ApiError(f"there is nothing in the library called {model_id}",
                       status=404)

    try:
        record, secret = store.issue(user.id, name, days=days, model_id=model_id)
    except TokenError as error:
        raise ApiError(error.message, status=error.status) from error
    return {"token": record.to_dict(), "secret": secret,
            "shown_once": True}


def api_token_revoke(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn one off. Somebody else's token is not yours to turn off."""
    store = tokens()
    user = require_user()
    token_id = str(_require(payload, "id"))
    try:
        record = store.get(token_id)
    except TokenError as error:
        raise ApiError(error.message, status=error.status) from error
    if record.user_id != user.id and not user.is_admin:
        raise ApiError("that token belongs to somebody else", status=403)
    store.revoke(token_id)
    return {"revoked": token_id, "tokens": [t.to_dict()
                                            for t in store.list(user.id)]}


def allowed_origins() -> tuple[str, ...]:
    """Which browser origins may call ``/v1``.

    The default depends on whether a token is required, and the difference
    matters. With accounts on, ``*`` is safe and useful: the credential is a
    bearer token the calling page had to be given, never a cookie the browser
    attaches by itself, so a page that has not been given one gets a 401
    whatever its origin. With accounts *off* there is no credential at all,
    and ``*`` would mean any page in the user's browser could read their
    library - so cross-origin is off entirely until somebody names an origin
    with ``--cors-origin``.
    """
    if CORS_ALLOW is not None:
        return CORS_ALLOW
    return ("*",) if AUTH is not None else ()


def cors_headers(origin: str) -> list[tuple[str, str]]:
    """The CORS headers for this origin, which may be none at all.

    Credentials are never allowed. That is not a limitation to be lifted
    later: it is what keeps ``Access-Control-Allow-Origin: *`` honest, since
    a browser will not send cookies to a surface that does not ask for them.
    """
    allowed = allowed_origins()
    if not allowed:
        return []
    if "*" in allowed:
        return [("Access-Control-Allow-Origin", "*")]
    if origin and origin in allowed:
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]
    return [("Vary", "Origin")]


def rest_caller(authorization: str) -> rest.Caller:
    """Who is making an integration request, and whose library they get.

    The session cookie is deliberately not consulted. ``/v1`` takes a bearer
    token or, when accounts are off, takes the one library this process has -
    and nothing in between.
    """
    if AUTH is None:
        return rest.Caller(store=library())

    presented = rest.bearer(authorization)
    if not presented:
        raise rest.RestError(
            "this endpoint needs an API token: send Authorization: Bearer "
            "flr_...", status=401, code="no_token")
    try:
        record = Tokens(DATABASE).verify(presented)
    except TokenError as error:
        raise rest.RestError(error.message, status=error.status,
                             code="bad_token") from error

    try:
        owner = AUTH.get(record.user_id)
    except AuthError as error:
        raise rest.RestError("that token's account is gone", status=401,
                             code="bad_token") from error
    if not owner.active:
        raise rest.RestError("that token's account is turned off", status=403,
                             code="account_off")
    return rest.Caller(store=DatabaseStore(DATABASE, owner=owner.id),
                       token=record)


def rest_dispatch(method: str, path: str, body: dict[str, Any],
                  authorization: str) -> rest.Response:
    """Run one ``/v1`` request, or say why not, without raising."""
    try:
        found = rest.match(method, path)
        if found is None:
            return rest.Response(404, {"error": f"no endpoint at {path}",
                                       "code": "not_found"})
        endpoint, params = found
        caller = rest_caller(authorization) if endpoint.needs_token else None
        answer = rest.Response(200, endpoint.handler(caller, params, body))
        # Stamped after the call, not before: "last used" should mean the
        # token did something, not that somebody probed an endpoint it has no
        # right to. A failed stamp is never worth failing the request over.
        if caller is not None and caller.token is not None:
            try:
                Tokens(DATABASE).touch(caller.token.id)
            except Exception:  # noqa: BLE001
                pass
        return answer
    except rest.RestError as error:
        return rest.Response(error.status, {"error": error.message,
                                            "code": error.code})
    except ApiError as error:
        return rest.Response(error.status, {"error": error.message,
                                            "code": "bad_request"})
    except Exception as error:  # noqa: BLE001 - one bad call must not end the server
        traceback.print_exc()
        return rest.Response(500, {"error": f"unexpected failure: {error}",
                                   "code": "server_error"})


# ----------------------------------------------------------------------
# Bot Builder: templates, and trying a conversation from the UI
# ----------------------------------------------------------------------


def _bot_reader(who: str):
    """The language-model reader for this owner, or None to read locally.

    Imported inside the function, like every other use of ``..llm``. The key
    comes from the owner's keyring row laid over the environment, the same
    as the rules feature, so a key typed into Settings works here too.
    """
    if not BOT_LLM:
        return None
    from ..llm import understand as llm_understand

    try:
        settings = _llm_settings("chat", who=who or SINGLE_USER)
    except ApiError:
        return None
    return llm_understand.reader(settings)


def _bot_transcriber(who: str):
    """The transcriber for this owner's OpenAI key, or None when it is off."""
    if not BOT_TRANSCRIBE:
        return None
    from ..llm import transcribe as llm_transcribe

    return llm_transcribe.transcriber(_llm_environ(who or SINGLE_USER))


def _template_from(payload: dict[str, Any]) -> Template:
    try:
        return Template.from_dict(_require(payload, "template"))
    except TemplateError as error:
        raise ApiError(str(error)) from None


def api_bot_templates(_: dict[str, Any]) -> dict[str, Any]:
    """Everything the Bots panel lists: templates, starters, and what to link."""
    store = library()
    have = bot_templates.listed(store)
    return {
        "templates": [t.card() for t in have],
        "starters": [t.card() for t in bot_templates.starters().values()],
        "schemas": [{"id": e.id, "name": e.name, "created": e.created}
                    for e in store.list(kind="schema", limit=100)],
        "models": [{"id": e.id, "name": e.name, "created": e.created}
                   for e in store.list(kind="model", limit=100)],
        "semantic_types": list(SEMANTIC_TYPES),
        "bot_llm": BOT_LLM,
        "bot_transcribe": BOT_TRANSCRIBE,
    }


def api_bot_template(payload: dict[str, Any]) -> dict[str, Any]:
    key = str(_require(payload, "key"))
    found = bot_templates.find(library(), key)
    if found is None:
        # The try-it chat can land on a starter that is not in the library
        # yet; the sample form still needs its fields.
        starter = bot_templates.starters().get(key)
        if starter is None:
            raise ApiError(f"there is no template called {key!r}", status=404)
        return {"template": starter.to_dict(), "entry_id": None, "starter": True}
    return {"template": found.to_dict(), "entry_id": found.entry_id}


def api_bot_save(payload: dict[str, Any]) -> dict[str, Any]:
    template = _template_from(payload)
    store = library()
    if template.model_id and not store.has(template.model_id):
        raise ApiError(f"there is no model called {template.model_id!r} to link")
    parent = payload.get("schema_id") or None
    saved = bot_templates.save(store, template, parent=parent)
    return {"template": saved.to_dict(), "entry_id": saved.entry_id}


def api_bot_delete(payload: dict[str, Any]) -> dict[str, Any]:
    key = str(_require(payload, "key"))
    gone = bot_templates.remove(library(), key)
    if not gone:
        raise ApiError(f"there is no template called {key!r}", status=404)
    return {"deleted": key, "entries": gone}


def api_bot_starter(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a bundled starter into this library, where it can be edited."""
    key = str(_require(payload, "key"))
    starter = bot_templates.starters().get(key)
    if starter is None:
        raise ApiError(f"there is no starter template called {key!r}", status=404)
    saved = bot_templates.save(library(), starter)
    return {"template": saved.to_dict(), "entry_id": saved.entry_id}


def api_bot_from_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """A draft template with a schema's fields. Not saved until it is edited."""
    if payload.get("id"):
        entry_id = _entry(payload)
        try:
            schema = library().load_schema(entry_id)
        except StoreError as error:
            raise ApiError(str(error)) from error
    else:
        schema, entry_id = _schema_from(payload), None
    try:
        draft = bot_templates.from_schema(schema)
    except TemplateError as error:
        raise ApiError(str(error)) from None
    return {"template": draft.to_dict(), "schema_id": entry_id}


def api_bot_transcribe(payload: dict[str, Any]) -> dict[str, Any]:
    """A recording from the Bots panel's microphone. Same code as ``/v1``."""
    try:
        return botrest.run_transcribe(_bot_transcriber(_who()), payload)
    except rest.RestError as error:
        raise ApiError(error.message, status=error.status) from None


def api_bot_turn(payload: dict[str, Any]) -> dict[str, Any]:
    """One turn, from the Bots panel's try-it chat. Same code as ``/v1``."""
    store = library()
    caller = rest.Caller(store=store)
    who = _who()
    # The starters the panel offers count too, so asking the chat for
    # "document request" works before anybody has added one; the page adds
    # it to the library when the chat picks it.
    saved = bot_templates.listed(store)
    have = {t.key for t in saved}
    available = saved + [t for k, t in bot_templates.starters().items() if k not in have]
    try:
        return botrest.run_turn(
            store, available, payload,
            load_model=lambda model_id: rest.load(caller, model_id)[0],
            reader=_bot_reader(who))
    except rest.RestError as error:
        raise ApiError(error.message, status=error.status) from None



# ----------------------------------------------------------------------
# help and documentation
# ----------------------------------------------------------------------
#
# The in-app help panel and the /docs site are made from the same Markdown.
# The help panel is part of the app and needs what the app needs, a session.
# The docs site is meant to be read by people who have never had an account -
# somebody the product page was shown to - so it is opened by an access code
# an administrator sets, and is closed until one is.

#: Where the access code's hash is kept in the database's settings table.
DOCS_SETTING = "docs_access"

#: The file it is kept in when there is no database (``--no-auth``).
DOCS_FILE = "docs-access.json"

#: The browser's proof that it was given the code. Scoped to /docs.
DOCS_COOKIE = "airforms_docs"
DOCS_COOKIE_DAYS = 30

MIN_DOCS_CODE = 6
MAX_DOCS_CODE = 128

#: Wrong codes allowed from one address before it has to wait. The code is
#: shared by design, so it is shorter than a password; this is what stops it
#: being guessed.
DOCS_MAX_FAILURES = 8
DOCS_WINDOW_SECONDS = 15 * 60

_DOCS_FAILURES: dict[str, list[float]] = {}
_DOCS_LOCK = threading.Lock()


def _docs_record() -> dict[str, Any] | None:
    """The stored code (hashed), who set it and when, or None."""
    try:
        if DATABASE is not None:
            raw = DATABASE.setting(DOCS_SETTING)
        else:
            path = Path(LIBRARY.root) / DOCS_FILE
            raw = path.read_text(encoding="utf-8") if path.is_file() else None
        record = json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("hash") else None


def _docs_store(record: dict[str, Any] | None) -> None:
    text = json.dumps(record) if record else ""
    if DATABASE is not None:
        DATABASE.remember(DOCS_SETTING, text)
        return
    path = Path(LIBRARY.root) / DOCS_FILE
    if record is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - not every filesystem has modes
        pass


def docs_cookie_for(record: dict[str, Any]) -> str:
    """The cookie value that proves the current code was given.

    Keyed by the stored hash, which never leaves the server, so it cannot be
    made without the code, and every cookie issued under an old code stops
    working the moment the code changes.
    """
    return hmac.new(record["hash"].encode("utf-8"), b"airforms-docs-access",
                    hashlib.sha256).hexdigest()


def docs_unlocked(cookie_value: str) -> bool:
    record = _docs_record()
    if record is None or not cookie_value:
        return False
    return hmac.compare_digest(cookie_value, docs_cookie_for(record))


def _docs_throttled(address: str) -> bool:
    now = time.monotonic()
    with _DOCS_LOCK:
        recent = [t for t in _DOCS_FAILURES.get(address, []) if now - t < DOCS_WINDOW_SECONDS]
        _DOCS_FAILURES[address] = recent
        return len(recent) >= DOCS_MAX_FAILURES


def _docs_failed(address: str) -> None:
    with _DOCS_LOCK:
        _DOCS_FAILURES.setdefault(address, []).append(time.monotonic())


_ADDRESS: ContextVar[str] = ContextVar("fillerai_address", default="")


def api_help(_: dict[str, Any]) -> dict[str, Any]:
    """The help panel's sections, one per screen, as HTML."""
    return {"sections": docs_site.help_sections()}


def api_docs_unlock(payload: dict[str, Any]) -> dict[str, Any]:
    address = _ADDRESS.get()
    if _docs_throttled(address):
        raise ApiError("too many wrong codes; try again in a few minutes", status=429)
    record = _docs_record()
    if record is None:
        raise ApiError("the documentation has no access code yet - an "
                       "administrator sets one in Settings", status=403)
    code = payload.get("code")
    if not isinstance(code, str) or not code or len(code) > MAX_DOCS_CODE \
            or not verify_password(record["hash"], code):
        _docs_failed(address)
        raise ApiError("that is not the access code", status=403)
    context().docs_cookie = docs_cookie_for(record)
    return {"ok": True, "next": "/docs"}


def api_docs_status(_: dict[str, Any]) -> dict[str, Any]:
    record = _docs_record()
    return {
        "configured": record is not None,
        "set_at": (record or {}).get("set_at", ""),
        "set_by": (record or {}).get("set_by", ""),
        "min_length": MIN_DOCS_CODE,
        "has_docs": bool(docs_site.catalog()),
    }


def api_docs_passcode(payload: dict[str, Any]) -> dict[str, Any]:
    """Set, generate or clear the documentation access code.

    ``{"code": "..."}`` sets it, ``{"generate": true}`` makes one up and
    returns it - the only time it is ever shown - and ``{"clear": true}``
    closes the docs to everybody.
    """
    if payload.get("clear"):
        _docs_store(None)
        return {"configured": False, "code": ""}
    if payload.get("generate"):
        code = suggest_password(3)
    else:
        code = payload.get("code")
        if not isinstance(code, str):
            raise ApiError("give the new access code")
        code = code.strip()
        if len(code) < MIN_DOCS_CODE:
            raise ApiError(f"use at least {MIN_DOCS_CODE} characters")
        if len(code) > MAX_DOCS_CODE:
            raise ApiError(f"use at most {MAX_DOCS_CODE} characters")
    user = current_user()
    record = {
        "hash": hash_password(code),
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "set_by": user.username if user else "",
    }
    _docs_store(record)
    status = api_docs_status({})
    status["code"] = code
    return status


@dataclass(frozen=True)
class Route:
    """An endpoint and who is allowed to reach it."""

    handler: Callable[[dict[str, Any]], dict[str, Any]]
    #: "" for anyone, "user" for anyone signed in, "admin" for an
    #: administrator. Enforced in one place, in :meth:`Handler.do_POST`,
    #: rather than by each handler remembering to ask.
    needs: str = "user"


#: Every endpoint, and who may reach it. The stages are open to anyone with
#: an account; the user list is not. Written here rather than as a decorator
#: on each handler so that the answer to "what can a signed-out request do"
#: is one list somebody can read.
ROUTES: dict[str, Route] = {
    "/api/meta": Route(api_meta, ""),
    "/api/auth/login": Route(api_login, ""),
    "/api/auth/logout": Route(api_logout, "user"),
    "/api/auth/me": Route(api_me, "user"),
    "/api/auth/password": Route(api_change_password, "user"),
    "/api/admin/users": Route(api_users, "admin"),
    "/api/admin/users/create": Route(api_user_create, "admin"),
    "/api/admin/users/update": Route(api_user_update, "admin"),
    "/api/admin/users/password": Route(api_user_password, "admin"),
    "/api/admin/users/delete": Route(api_user_delete, "admin"),
    "/api/admin/database": Route(api_admin_database, "admin"),
    "/api/admin/import": Route(api_admin_import, "admin"),
    "/api/example": Route(api_example),
    "/api/extract": Route(api_extract),
    "/api/reinfer": Route(api_reinfer),
    "/api/generate": Route(api_generate),
    "/api/export": Route(api_export),
    "/api/data/read": Route(api_data_read),
    "/api/data/clean": Route(api_data_clean),
    "/api/data/schema": Route(api_data_schema),
    "/api/evaluate": Route(api_evaluate),
    "/api/train": Route(api_train),
    "/api/train/start": Route(api_train_start),
    "/api/train/log": Route(api_train_log),
    "/api/train/script": Route(api_train_script),
    "/api/train/tree": Route(api_train_tree),
    "/api/predict": Route(api_predict),
    "/api/library": Route(api_library),
    "/api/library/open": Route(api_library_open),
    "/api/library/export": Route(api_library_export),
    "/api/library/delete": Route(api_library_delete),
    "/api/library/rename": Route(api_library_rename),
    "/api/model": Route(api_model),
    "/api/simulate/form": Route(api_simulate_form),
    "/api/simulate/case": Route(api_simulate_case),
    "/api/simulate/fill": Route(api_simulate_fill),
    "/api/simulate/sweep": Route(api_simulate_sweep),
    "/api/llm/status": Route(api_llm_status),
    "/api/llm/key": Route(api_llm_key),
    "/api/llm/preference": Route(api_llm_preference),
    "/api/llm/forget": Route(api_llm_forget),
    "/api/llm/rules/estimate": Route(api_llm_rules_estimate),
    "/api/llm/rules/propose": Route(api_llm_rules_propose),
    "/api/llm/rules/apply": Route(api_llm_rules_apply),
    "/api/bot/templates": Route(api_bot_templates),
    "/api/bot/template": Route(api_bot_template),
    "/api/bot/save": Route(api_bot_save),
    "/api/bot/delete": Route(api_bot_delete),
    "/api/bot/starter": Route(api_bot_starter),
    "/api/bot/from_schema": Route(api_bot_from_schema),
    "/api/bot/turn": Route(api_bot_turn),
    "/api/bot/transcribe": Route(api_bot_transcribe),
    "/api/help": Route(api_help),
    "/api/docs/unlock": Route(api_docs_unlock, ""),
    "/api/admin/docs": Route(api_docs_status, "admin"),
    "/api/admin/docs/passcode": Route(api_docs_passcode, "admin"),
    "/api/tokens": Route(api_tokens),
    "/api/tokens/create": Route(api_token_create),
    "/api/tokens/revoke": Route(api_token_revoke),
}


# ----------------------------------------------------------------------
# the server
# ----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"AIrForms/{__version__}"
    # Quiet by default; the console is for the user's own output.
    quiet = True
    # Set by ``serve --trust-proxy``: a platform such as Railway puts its own
    # proxy in front, so the connection always comes from that proxy and the
    # visitor's address and scheme are only in the headers it adds. Off by
    # default, because without such a proxy those headers are whatever the
    # visitor chose to send.
    trust_proxy = False

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        finally:
            # A request's thread is about to end; a pooled backend wants its
            # connection back rather than one dropped per request.
            if DATABASE is not None:
                DATABASE.release()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not self.quiet:
            super().log_message(fmt, *args)

    # -- helpers --------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str,
              extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra or ():
            self.send_header(name, value)
        # This is a local tool; nothing here should be embedded elsewhere.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   self._cookie_headers())

    # -- the session cookie ---------------------------------------------

    def _cookie_headers(self) -> list[tuple[str, str]]:
        """A Set-Cookie, if signing in or out happened while handling this.

        HttpOnly so no script on the page can read it, SameSite=Strict so it
        is not attached to a request another site started, and no Secure
        flag because this is served over http on loopback and a Secure cookie
        would simply never be stored.
        """
        headers = []
        # Behind a trusted proxy that took the request over https, Secure is
        # both possible and right: the cookie then never travels in the clear.
        secure = "; Secure" if self._forwarded_https() else ""
        value = context().cookie
        if value == "":
            headers.append(("Set-Cookie",
                            f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; "
                            f"SameSite=Strict{secure}"))
        elif value is not None:
            headers.append(("Set-Cookie",
                            f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict{secure}"))
        docs = context().docs_cookie
        if docs:
            # Lax rather than Strict: following a link to the docs from
            # somewhere else should arrive signed in to them. Nothing under
            # /docs changes anything, so there is nothing for Lax to expose.
            headers.append(("Set-Cookie",
                            f"{DOCS_COOKIE}={docs}; Path=/docs; "
                            f"Max-Age={DOCS_COOKIE_DAYS * 86400}; HttpOnly; "
                            f"SameSite=Lax{secure}"))
        return headers

    def _forwarded_https(self) -> bool:
        if not self.trust_proxy:
            return False
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0]
        return proto.strip().lower() == "https"

    def _visitor_address(self) -> str:
        """Who is asking: the connection, or what a trusted proxy says.

        X-Real-IP first, then the last X-Forwarded-For entry - the one the
        proxy itself appended, not the ones the visitor may have sent ahead.
        """
        if self.trust_proxy:
            real = (self.headers.get("X-Real-IP") or "").strip()
            if real:
                return real
            chain = [a.strip() for a in
                     (self.headers.get("X-Forwarded-For") or "").split(",") if a.strip()]
            if chain:
                return chain[-1]
        return self.client_address[0] if self.client_address else ""

    def _session_cookie(self, name: str = COOKIE) -> str:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is no cookie
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _sign_in(self) -> Context:
        """Work out who is asking, before deciding whether they may."""
        holder = Context()
        _ADDRESS.set(self._visitor_address())
        if AUTH is not None:
            found = AUTH.resolve(self._session_cookie())
            if found is not None:
                holder.session, holder.user = found
        _CONTEXT.set(holder)
        return holder

    def _guard(self, path: str, route: "Route") -> dict[str, Any] | None:
        """``None`` to go ahead, or the refusal to send instead.

        One place, checked for every POST, rather than each handler
        remembering: forgetting once is the whole failure.
        """
        if AUTH is None:
            return None
        holder = context()
        if route.needs and holder.user is None:
            return {"status": 401,
                    "body": {"error": "sign in to use AIrForms",
                             "sign_in": True}}
        if route.needs == "admin" and not (holder.user and holder.user.is_admin):
            return {"status": 403,
                    "body": {"error": "that is an administrator's to do"}}
        if holder.user is not None and holder.user.must_change \
                and path not in WHILE_LOCKED:
            return {"status": 403,
                    "body": {"error": "choose a new password before carrying on",
                             "must_change": True}}
        # The token is checked on everything with a session behind it. The
        # login itself has no session to check against, and is safe without
        # one: it carries the password, which is the thing an attacker who
        # could forge the request does not have.
        if holder.session is not None and path not in PUBLIC:
            sent = self.headers.get(CSRF_HEADER) or ""
            if not hmac.compare_digest(sent, holder.session.csrf):
                return {"status": 403,
                        "body": {"error": "this page is out of date - reload it",
                                 "stale": True}}
        return None

    def _drain(self, remaining: int) -> None:
        """Read and discard a bounded part of the request body."""
        while remaining > 0:
            chunk = self.rfile.read(min(DRAIN_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _redirect(self, where: str) -> None:
        self._send(302, b"", "text/plain; charset=utf-8",
                   [("Location", where)] + self._cookie_headers())

    def _static(self, path: str, root: Path = STATIC_DIR,
                default: str = "index.html") -> None:
        relative = path.lstrip("/") or default
        target = (root / relative).resolve()
        # Resolve first, then confirm the result is still inside the served
        # directory, so "../" cannot escape it.
        if not target.is_file() or root.resolve() not in target.parents:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    def _send_html(self, status: int, page: str) -> None:
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _docs(self, path: str) -> None:
        """The documentation site, or the page that asks for its code."""
        record = _docs_record()
        if record is None or not docs_unlocked(self._session_cookie(DOCS_COOKIE)):
            self._send_html(200 if record is not None else 403,
                            docs_site.gate_page(record is not None))
            return
        catalog = docs_site.catalog()
        slug = path[len("/docs"):].strip("/").lower()
        if slug.endswith(".md"):
            slug = slug[:-3]
        if not slug:
            self._send_html(200, docs_site.home_page(catalog))
            return
        doc = catalog.get(slug)
        if doc is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send_html(200, docs_site.doc_page(catalog, doc))

    # -- the integration surface ----------------------------------------

    def _rest(self, method: str) -> None:
        """Handle a ``/v1`` request: bearer tokens, CORS, no cookie at all.

        A fresh empty context is installed first, so that nothing underneath
        can accidentally read a signed-in user off this request. Who the
        caller is comes from the Authorization header or from nowhere.
        """
        _CONTEXT.set(Context())
        path = self.path.split("?", 1)[0]
        extra = cors_headers(self.headers.get("Origin") or "")

        body: dict[str, Any] = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_rest(400, {"error": "bad Content-Length",
                                      "code": "bad_request"}, extra)
                return
            if length > MAX_BODY_BYTES:
                self._drain(min(length, DRAIN_LIMIT))
                self._send_rest(413, {"error": "that request is too large",
                                      "code": "too_large"}, extra)
                self.close_connection = True
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_rest(400, {"error": f"request body is not valid "
                                               f"JSON: {error}",
                                      "code": "bad_json"}, extra)
                return
            if not isinstance(body, dict):
                self._send_rest(400, {"error": "request body must be a JSON object",
                                      "code": "bad_json"}, extra)
                return

        answer = rest_dispatch(method, path, body,
                               self.headers.get("Authorization") or "")
        self._send_rest(answer.status, answer.body, extra + answer.headers)

    def _send_rest(self, status: int, body: dict[str, Any],
                   extra: list[tuple[str, str]]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8", extra)

    def _preflight(self) -> None:
        """Answer the OPTIONS a browser sends before a cross-origin call.

        Only for ``/v1``. The UI's own endpoints are same-origin by
        construction and have no business answering a preflight.
        """
        extra = cors_headers(self.headers.get("Origin") or "")
        if not extra or not any(name == "Access-Control-Allow-Origin"
                                for name, _ in extra):
            # Nothing is allowed from here. Saying so plainly is better than
            # a 200 the browser then refuses to act on.
            self._send(403, b"", "text/plain; charset=utf-8", extra)
            return
        self._send(204, b"", "text/plain; charset=utf-8", extra + [
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
            ("Access-Control-Max-Age", "600"),
        ])

    # -- verbs ----------------------------------------------------------

    def _sample(self, path: str) -> None:
        """Pass a /sample/ request through to the sample application.

        It runs in this process on its own port; this is what lets it be
        reached on the one port a hosting platform exposes. Signed-in people
        only, unless opened up: its chat speaks with an administrator's token.
        """
        if SAMPLE_APP_PORT is None:
            self._send(404, b"the sample application is not running here",
                       "text/plain; charset=utf-8")
            return
        holder = self._sign_in()
        if AUTH is not None and holder.user is None and not SAMPLE_PUBLIC:
            if self.command == "GET":
                self._redirect("/login")
            else:
                self._send_json(401, {"error": "sign in to AIrForms first"})
            return
        if path == SAMPLE_PATH:
            # Its pages use relative links, which need the trailing slash.
            self._redirect(f"{SAMPLE_PATH}/")
            return
        import http.client

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        target = self.path[len(SAMPLE_PATH):] or "/"
        headers = {"X-Forwarded-Prefix": SAMPLE_PATH}
        for name in ("Content-Type", "Accept"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        connection = http.client.HTTPConnection("127.0.0.1", SAMPLE_APP_PORT, timeout=120)
        try:
            method = "GET" if self.command == "HEAD" else self.command
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            kind = response.getheader("Content-Type") or "application/octet-stream"
            extra = [("Location", f"{SAMPLE_PATH}{response.getheader('Location')}")] \
                if (response.getheader("Location") or "").startswith("/") else []
            self._send(response.status, payload, kind, extra)
        except OSError as error:
            self._send_json(502, {"error": f"the sample application did not answer: {error}"})
        finally:
            connection.close()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == SAMPLE_PATH or path.startswith(f"{SAMPLE_PATH}/"):
            self._sample(path)
            return
        # The integration API first, and without signing anybody in: it is a
        # different surface with a different credential, and letting a cookie
        # reach it would undo the reason it has one.
        if path == "/v1" or path.startswith(rest.PREFIX):
            self._rest("GET")
            return

        holder = self._sign_in()
        signed_in = AUTH is None or holder.user is not None

        if path == "/":
            # The product page: public, and the front door for somebody who
            # has never seen AIrForms. It links to the login and the docs.
            self._static("product.html")
        elif path in ("/app", "/app/"):
            # The app itself is behind the login, so that a signed-out
            # browser lands on the page it can do something with rather than
            # on a stage that will refuse every button.
            self._static("index.html") if signed_in else self._redirect("/login")
        elif path == "/index.html":
            # Where the app used to live, for bookmarks made before /app.
            self._redirect("/app")
        elif path == "/login":
            if AUTH is None:
                self._redirect("/app")
            elif holder.user is not None and not holder.user.must_change:
                self._redirect("/app")
            else:
                self._static("login.html")
        elif path == "/docs" or path.startswith("/docs/"):
            self._docs(path)
        elif path.startswith("/static/"):
            # Stylesheet, script and the login page's own assets: served
            # signed out, because the login page is made of them.
            self._static(path[len("/static/"):])
        elif path == "/client" or path.startswith("/client/"):
            # The JavaScript client and its demo page. Served signed out and
            # to anyone, because they are inert: the client is a wrapper
            # around fetch, and everything it wraps needs a token.
            self._static(path[len("/client"):], root=CLIENT_DIR, default="demo.html")
        elif path == "/api/meta":
            self._send_json(200, api_meta({}))
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET  # noqa: N815

    def do_OPTIONS(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/v1" or path.startswith(rest.PREFIX):
            self._preflight()
            return
        self._send(404, b"", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith(f"{SAMPLE_PATH}/"):
            self._sample(path)
            return
        if path == "/v1" or path.startswith(rest.PREFIX):
            self._rest("POST")
            return

        route = ROUTES.get(path)
        if route is None:
            self._send_json(404, {"error": f"no endpoint at {path}"})
            return

        self._sign_in()
        refusal = self._guard(path, route)
        if refusal is not None:
            # Still drain, or a browser that was mid-upload gets a broken
            # pipe instead of the reason it was turned away.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            self._drain(min(length, DRAIN_LIMIT))
            self._send_json(refusal["status"], refusal["body"])
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            # The client is still sending. Replying without reading leaves it
            # with a broken pipe instead of the message, so the body is drained
            # first - bounded, since the point is not to read it all.
            self._drain(min(length, DRAIN_LIMIT))
            self._send_json(413, {"error": "that source is too large for the UI"})
            self.close_connection = True
            return

        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": f"request body is not valid JSON: {error}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "request body must be a JSON object"})
            return

        try:
            self._send_json(200, route.handler(payload))
        except ApiError as error:
            self._send_json(error.status, {"error": error.message})
        except Exception as error:  # noqa: BLE001 - the UI must not die on one bad request
            traceback.print_exc()
            self._send_json(500, {"error": f"unexpected failure: {error}"})


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def open_database(url: str | None = None, *, accounts: bool = True) -> Database:
    """Open the database this process will use, and set up accounts on it.

    Called by :func:`serve` and by the tests. Migrations run every time and
    do nothing when there is nothing to do, so an old database catches up on
    start rather than needing a command somebody has to know about.
    """
    global DATABASE, AUTH

    DATABASE = connect_database(url, library_root=LIBRARY.root)
    AUTH = Auth(DATABASE) if accounts else None
    if AUTH is not None:
        AUTH.sweep()
    return DATABASE


def close_database() -> None:
    """Put the process back to no database and no accounts. Tests want this."""
    global DATABASE, AUTH

    if DATABASE is not None:
        DATABASE.close()
    DATABASE, AUTH = None, None
    # Models loaded out of that database belong to it, not to the process.
    rest.forget()


def _first_run(auth: Auth) -> None:
    """Make an administrator if there is nobody, and say so loudly.

    A tool nobody can sign in to is not a tool. The password is printed once,
    to the console of the person who started the server - who is the person
    setting it up - and is not stored anywhere in the clear. It has to be
    changed at first sign-in.
    """
    if auth.count():
        return
    try:
        user, generated = auth.bootstrap()
    except AuthError as exc:
        # Most likely $FILLERAI_ADMIN_PASSWORD is too short. Say which, rather
        # than a traceback in a hosting platform's log.
        raise SystemExit(f"Could not create the first administrator: {exc}.\n"
                         f"Check $FILLERAI_ADMIN_PASSWORD.") from None
    print("")
    print("  No accounts yet, so one administrator was created:")
    print(f"    username: {user.username}")
    if generated:
        print(f"    password: {generated}")
        print("    (shown once; you will be asked to change it when you sign in)")
    else:
        print("    password: the one in $FILLERAI_ADMIN_PASSWORD")
    print("")


def _offer_import() -> None:
    """Bring an existing file library in, the first time there is a database.

    Somebody who has been using AIrForms has a ``.fillerai`` directory full
    of their work. Starting the server with accounts should not look like
    losing it, so it is copied into the first administrator's library, ids
    and lineage intact, and said out loud. Running again copies nothing,
    because every id is already there.
    """
    if DATABASE is None or AUTH is None:
        return
    if not Path(str(LIBRARY.root)).is_dir() or not LIBRARY.list(limit=1):
        return
    admins = [u for u in AUTH.users() if u.is_admin and u.active]
    if not admins:
        return
    target = DatabaseStore(DATABASE, owner=admins[0].id)
    result = import_store(LIBRARY, target)
    if result["copied"]:
        print(f"  imported {len(result['copied'])} entries from {LIBRARY.root}"
              f" into {admins[0].username}'s library")


def _is_loopback(host: str) -> bool:
    """Is this address reachable only from this machine?

    Used to decide where running without accounts is allowed at all. An
    unparseable host, or the empty one, is not loopback: an empty bind
    address means every interface, which is the case this is guarding.
    """
    name = (host or "").strip().lower()
    if name in ("localhost", "[::1]"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def use_bot_llm(on: bool) -> None:
    """Turn the language-model reading of chat phrases on or off."""
    global BOT_LLM
    BOT_LLM = bool(on)
    botrest.READER = _bot_reader if BOT_LLM else (lambda owner: None)


def use_bot_transcribe(on: bool) -> None:
    """Turn transcribing recordings with an OpenAI key on or off."""
    global BOT_TRANSCRIBE
    BOT_TRANSCRIBE = bool(on)
    botrest.TRANSCRIBER = _bot_transcriber if BOT_TRANSCRIBE else (lambda owner: None)


def _flag(value: bool | None, variable: str) -> bool:
    if value is not None:
        return value
    return os.environ.get(variable, "") not in ("", "0", "false", "no")


def start_sample_app(host: str, fillerai_port: int, port: int = 8100,
                     username: str | None = None) -> tuple[Any, str]:
    """Start the sample application next to this server, in a thread.

    It is an outside application that happens to share the process: it
    reaches AIrForms only over HTTP on ``/v1``, with an API token issued here
    for one account (``username``, or the first administrator), so its forms
    are that account's bot templates. The token is replaced on every start,
    and nobody has to copy one anywhere. Returns the server and a line to
    print; the server is ``None`` when it could not start, and the line says
    why.
    """
    from ..sampleapp import app as sample

    secret, whose, owner = "", "", ""
    if AUTH is not None and DATABASE is not None:
        user = AUTH.find(username) if username else next(
            (u for u in AUTH.users() if u.is_admin and u.active), None)
        if user is None:
            return None, (f"sample application: not started, there is no user "
                          f"{username!r}" if username else
                          "sample application: not started, there is no administrator")
        store = Tokens(DATABASE)
        while (old := store.find(user.id, SAMPLE_APP_TOKEN)) is not None:
            store.forget(old.id)
        _, secret = store.issue(user.id, SAMPLE_APP_TOKEN)
        whose, owner = f", as {user.username}", user.username
    local = "127.0.0.1" if host in ("0.0.0.0", "", "localhost") else host
    base = f"http://{'[' + local + ']' if ':' in local else local}:{fillerai_port}"
    # The demo customer's record: the sample application's own database.
    data = Path(LIBRARY.root) / "sample-app" / (f"portal-{owner}.json" if owner else "portal.json")
    try:
        httpd = sample.make_server(sample.FillerAIService(base, secret),
                                   sample.Portal(data), host=host, port=port,
                                   server_speech=BOT_TRANSCRIBE, fillerai_page=base + "/")
    except OSError as error:
        return None, (f"sample application: not started, port {port} is taken "
                      f"({error.strerror}); use --sample-port, or --no-sample-app")
    httpd.handler.quiet = True
    threading.Thread(target=httpd.serve_forever, name="sample-app", daemon=True).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    return httpd, (f"sample application: http://{shown}:{httpd.server_address[1]}/  "
                   f"(its forms are the bot templates{whose})")


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False,
          verbose: bool = False, library_path: str | None = None,
          database: str | None = None, accounts: bool = True,
          cors_origins: list[str] | None = None, bot_llm: bool | None = None,
          bot_transcribe: bool | None = None, sample_app: bool = True,
          sample_port: int = 8100, sample_user: str | None = None,
          trust_proxy: bool = False, sample_public: bool | None = None) -> int:
    global LIBRARY, CORS_ALLOW, SAMPLE_APP_PORT, SAMPLE_PUBLIC

    Handler.quiet = not verbose
    Handler.trust_proxy = trust_proxy
    SAMPLE_PUBLIC = _flag(sample_public, SAMPLE_PUBLIC_VARIABLE)
    use_bot_llm(_flag(bot_llm, BOT_LLM_VARIABLE))
    use_bot_transcribe(_flag(bot_transcribe, BOT_TRANSCRIBE_VARIABLE))
    if cors_origins is not None:
        CORS_ALLOW = tuple(o.strip() for o in cors_origins if o.strip())

    # Without accounts, everyone who can reach the port is signed in, so the
    # port had better only be reachable from this machine. A printed warning
    # was the old answer and it is the wrong one: the person who needs to
    # read it is already not reading the console.
    if not accounts and not _is_loopback(host):
        print(f"--no-auth means anyone who can reach the port is signed in, so "
              f"it is only allowed on localhost, and --host {host} is not.\n"
              f"Either drop --no-auth and sign in, or keep --host 127.0.0.1.",
              file=sys.stderr)
        return 1

    if library_path:
        LIBRARY = Store(library_path)

    if accounts or database:
        open_database(database, accounts=accounts)
        if AUTH is not None:
            _first_run(AUTH)
            _offer_import()

    httpd = create_server(host, port)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{httpd.server_address[1]}/"
    sample_httpd, sample_line = (None, "")
    if sample_app:
        sample_httpd, sample_line = start_sample_app(host, httpd.server_address[1],
                                                     sample_port, sample_user)
        if sample_httpd is not None:
            SAMPLE_APP_PORT = sample_httpd.server_address[1]

    print(f"AIrForms on {url}  (app: {url}app, docs: {url}docs)")
    public = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if public:
        print(f"  public address: https://{public}/")
    on_disk = DATABASE is None or DATABASE.url.startswith("sqlite://")
    if on_disk and os.environ.get("RAILWAY_ENVIRONMENT") and \
            not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        print("  WARNING: no Railway volume is attached, so accounts and the "
              "library are lost at every deploy. Attach one at "
              f"{LIBRARY.root}.")
    if DATABASE is not None:
        print(f"  database: {safe_url(DATABASE.url)}")
    else:
        print(f"  library: {LIBRARY.root}")
    if AUTH is None:
        print("  accounts: off - anyone who can reach this port is signed in")
    else:
        print(f"  accounts: on, {AUTH.count()} user(s)")
    origins = allowed_origins()
    print(f"  integration API: {url}v1  "
          f"({'browsers: ' + ', '.join(origins) if origins else 'same origin only'})")
    print(f"  JavaScript client: {url}client/fillerai.js")
    print(f"  chat demo: {url}client/chat.html  (bot reads phrases "
          f"{'with a language model' if BOT_LLM else 'on this machine'})")
    if BOT_TRANSCRIBE:
        from ..llm import transcribe as llm_transcribe

        have = "an OpenAI key is set" if llm_transcribe.openai_key() else \
            "no OpenAI key in the environment yet; add one in Settings"
        print(f"  microphone: recordings are transcribed with OpenAI ({have})")
    if sample_line:
        print(f"  {sample_line}")
    if SAMPLE_APP_PORT:
        print(f"  sample application here too: {url}sample/  "
              f"({'open to anyone' if SAMPLE_PUBLIC or AUTH is None else 'for signed-in people'})")
    print("  press Ctrl-C to stop")

    if open_browser:
        import webbrowser

        webbrowser.open(f"{url}app")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if sample_httpd is not None:
            sample_httpd.shutdown()
            sample_httpd.server_close()
        httpd.server_close()
        close_database()
    return 0
