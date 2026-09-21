"""A local HTTP server for the FillerAI UI.

Built on ``http.server`` so the UI inherits the same promise as the rest of
the project: nothing to install, nothing that phones home. It is a local
working tool, not a public service, so it binds to the loopback interface by
default and says so loudly if asked to do otherwise.

The API is deliberately thin. Every endpoint is a direct call into the same
functions ``fillerai.cli`` uses, which is what keeps the UI and the CLI from
drifting apart as the later phases land.

**Accounts are optional and on by default.** ``fillerai serve`` opens a
database, makes an administrator if there is nobody, and asks for a login;
``--no-auth`` skips all of it and is the single-user tool this started as.
Both paths run the same endpoints - what changes is which library
:func:`library` hands back, and whether :meth:`Handler._guard` lets the
request through at all.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import random
import sys
import threading
import traceback
import uuid
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import __version__, extract_html, extract_spec
from ..auth import (
    COOKIE, ROLES, Auth, AuthError, Session, User, suggest_password,
)
from ..db import Database, connect as connect_database
from ..dbstore import DatabaseStore, import_store
from ..extract import html_form, spec as spec_loader
from ..generate.dataset import Dataset, Options, coherence_report, generate, validate
from ..infer import infer
from ..schema import SEMANTIC_TYPES, FormSchema
from ..simulate.effort import DEFAULT_EFFORT
from ..simulate.form import layout as form_layout
from ..simulate.run import run as simulate, sweep as simulate_many
from ..store import KINDS, Store, StoreError
from ..train import algos, script as script_writer
from ..train.evaluate import evaluate, suggest_seed_fields
from ..train.model import ACCEPT_ABOVE, AutofillModel, TrainOptions, split_records, train
from ..train.trace import Trace

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"

# A local tool still needs limits: a runaway request should fail cleanly
# rather than exhaust the process.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 5000

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

#: The header a browser must echo the session's token in. A cookie alone
#: would let any page on the machine POST here with the user's credentials
#: attached; a token another origin cannot read is what stops that. Sent by
#: the UI on every call, which is why it is a header and not a form field.
CSRF_HEADER = "X-FillerAI-Token"

#: Endpoints reachable without signing in. Everything else needs a session
#: whenever accounts are on.
PUBLIC = {"/api/auth/login", "/api/meta"}

#: Endpoints a user who must change their password may still call. Anything
#: else would be working in an account somebody else knows the password to.
WHILE_LOCKED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout",
                "/api/meta"}


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
# training
# ----------------------------------------------------------------------

_MODELS: "OrderedDict[str, AutofillModel]" = OrderedDict()


def _remember(model: AutofillModel) -> str:
    token = uuid.uuid4().hex[:16]
    _MODELS[token] = model
    while len(_MODELS) > MAX_CACHED_MODELS:
        _MODELS.popitem(last=False)
    return token


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
        def work() -> None:
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


def api_simulate_form(payload: dict[str, Any]) -> dict[str, Any]:
    """The form to draw, and what the model would like typed into it."""
    model = _recall(str(_require(payload, "model_id")))
    return {
        "layout": form_layout(model.schema).to_dict(),
        "seeds": suggest_seed_fields(model, _bounded_int(payload, "ask", 3, 1, 8)),
        "threshold": ACCEPT_ABOVE,
        "effort": DEFAULT_EFFORT.to_dict(),
        "assumptions": DEFAULT_EFFORT.assumptions(),
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
            out["model_id"] = _remember(model)
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
}


# ----------------------------------------------------------------------
# the server
# ----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"FillerAI/{__version__}"
    # Quiet by default; the console is for the user's own output.
    quiet = True

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
        value = context().cookie
        if value is None:
            return []
        if value == "":
            return [("Set-Cookie",
                     f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")]
        return [("Set-Cookie",
                 f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict")]

    def _session_cookie(self) -> str:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is no cookie
            return ""
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else ""

    def _sign_in(self) -> Context:
        """Work out who is asking, before deciding whether they may."""
        holder = Context()
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
                    "body": {"error": "sign in to use FillerAI",
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

    def _static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        target = (STATIC_DIR / relative).resolve()
        # Resolve first, then confirm the result is still inside the static
        # directory, so "../" cannot escape it.
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    # -- verbs ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        holder = self._sign_in()
        signed_in = AUTH is None or holder.user is not None

        if path in ("/", "/index.html"):
            # The app itself is behind the login, so that a signed-out
            # browser lands on the page it can do something with rather than
            # on a stage that will refuse every button.
            self._static("index.html") if signed_in else self._redirect("/login")
        elif path == "/login":
            if AUTH is None:
                self._redirect("/")
            elif holder.user is not None and not holder.user.must_change:
                self._redirect("/")
            else:
                self._static("login.html")
        elif path.startswith("/static/"):
            # Stylesheet, script and the login page's own assets: served
            # signed out, because the login page is made of them.
            self._static(path[len("/static/"):])
        elif path == "/api/meta":
            self._send_json(200, api_meta({}))
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
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


def _first_run(auth: Auth) -> None:
    """Make an administrator if there is nobody, and say so loudly.

    A tool nobody can sign in to is not a tool. The password is printed once,
    to the console of the person who started the server - who is the person
    setting it up - and is not stored anywhere in the clear. It has to be
    changed at first sign-in.
    """
    if auth.count():
        return
    user, generated = auth.bootstrap()
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

    Somebody who has been using FillerAI has a ``.fillerai`` directory full
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


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False,
          verbose: bool = False, library_path: str | None = None,
          database: str | None = None, accounts: bool = True) -> int:
    global LIBRARY

    Handler.quiet = not verbose

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

    print(f"FillerAI UI on {url}")
    if DATABASE is not None:
        print(f"  database: {DATABASE.url}")
    else:
        print(f"  library: {LIBRARY.root}")
    if AUTH is None:
        print("  accounts: off - anyone who can reach this port is signed in")
    else:
        print(f"  accounts: on, {AUTH.count()} user(s)")
    print("  press Ctrl-C to stop")

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        close_database()
    return 0
