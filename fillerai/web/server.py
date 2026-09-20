"""A local HTTP server for the FillerAI UI.

Built on ``http.server`` so the UI inherits the same promise as the rest of
the project: nothing to install, nothing that phones home. It is a local
working tool, not a public service, so it binds to the loopback interface by
default and says so loudly if asked to do otherwise.

The API is deliberately thin. Every endpoint is a direct call into the same
functions ``fillerai.cli`` uses, which is what keeps the UI and the CLI from
drifting apart as the later phases land.
"""

from __future__ import annotations

import json
import mimetypes
import random
import threading
import traceback
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import __version__, extract_html, extract_spec
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


#: One library per server process, in the directory the server was started
#: from unless $FILLERAI_HOME says otherwise. Resolved once at import so
#: every endpoint agrees about where it is, and so a user who reads the path
#: off the Library panel can go and look at the same files.
LIBRARY = Store.default()


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
    """Everything the front end needs to render its controls."""
    return {
        "version": __version__,
        "semantic_types": list(SEMANTIC_TYPES),
        "examples": _list_examples(),
        "algorithms": [a.to_dict() for a in algos.all_algorithms()],
        "default_algorithm": algos.DEFAULT,
        "library": str(LIBRARY.root),
    }


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
        source = LIBRARY.save_source(content, kind=kind, name=name)
        out["source_id"] = source.id
        out["schema_id"] = LIBRARY.save_schema(schema, parent=source.id).id
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
        out["dataset_id"] = LIBRARY.save_dataset(
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
    if given and LIBRARY.has(str(given)):
        return str(given)
    return LIBRARY.save_schema(schema).id


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
        if not parent or not LIBRARY.has(str(parent)):
            schema_id = _parent_schema(payload, model.schema)
            parent = LIBRARY.save_dataset(
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
        result["model_entry_id"] = LIBRARY.save_model(
            model, parent=str(parent),
            name=f"{model.schema.name or 'form'}: {options.algorithm}",
            meta={"seeds": seeds, **scores},
        ).id
        result["dataset_id"] = str(parent)
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
    if not LIBRARY.has(entry_id):
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
    entries = LIBRARY.list(kind=kind, parent=payload.get("under"), limit=limit)
    return {
        "root": str(LIBRARY.root),
        "totals": LIBRARY.totals(),
        "bytes": LIBRARY.size(),
        "entries": [
            dict(entry.to_dict(),
                 lineage=[a.id for a in LIBRARY.lineage(entry.id)[:-1]],
                 children=[c.id for c in LIBRARY.children(entry.id)])
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
    entry = LIBRARY.get(entry_id)
    lineage = LIBRARY.lineage(entry_id)
    out: dict[str, Any] = {
        "entry": entry.to_dict(),
        "lineage": [a.to_dict() for a in lineage],
        "children": [c.to_dict() for c in LIBRARY.children(entry_id)],
    }

    # Whatever is above this entry, so the panel can restore the whole chain
    # and not just the leaf.
    for ancestor in lineage:
        if ancestor.kind == "schema":
            out["schema"] = LIBRARY.payload(ancestor.id)
            out["schema_id"] = ancestor.id
        elif ancestor.kind == "source":
            payload_body = LIBRARY.payload(ancestor.id)
            out["source"] = payload_body.get("content", "")
            out["source_kind"] = payload_body.get("kind", "html")
            out["source_id"] = ancestor.id
        elif ancestor.kind == "dataset":
            out["dataset_id"] = ancestor.id

    try:
        if entry.kind == "dataset":
            out["records"] = LIBRARY.load_records(entry_id)
        elif entry.kind == "model":
            model = LIBRARY.load_model(entry_id)
            out["model_id"] = _remember(model)
            out["algorithm"] = model.algorithm
            out["seeds"] = entry.meta.get("seeds") or suggest_seed_fields(model, 3)
            out["fields"] = model.field_report()
            out["drawable"] = sorted(getattr(model.engine, "trees", {}))
            out["engine"] = model.engine.summary()
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
    """One entry as the file it would be on disk."""
    entry_id = _entry(payload)
    entry = LIBRARY.get(entry_id)
    suffix = {"source": "source", "schema": "schema", "dataset": "records",
              "model": "model"}[entry.kind]
    return {
        "filename": f"{entry.id}.{suffix}.json",
        "text": json.dumps(LIBRARY.payload(entry_id), indent=2, ensure_ascii=False),
    }


def api_library_delete(payload: dict[str, Any]) -> dict[str, Any]:
    entry_id = _entry(payload)
    try:
        removed = LIBRARY.delete(entry_id, cascade=bool(payload.get("cascade")))
    except StoreError as error:
        raise ApiError(str(error)) from error
    return {"removed": removed}


def api_library_rename(payload: dict[str, Any]) -> dict[str, Any]:
    entry_id = _entry(payload)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError("a name cannot be empty")
    return {"entry": LIBRARY.rename(entry_id, name[:120]).to_dict()}


ROUTES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "/api/meta": api_meta,
    "/api/example": api_example,
    "/api/extract": api_extract,
    "/api/reinfer": api_reinfer,
    "/api/generate": api_generate,
    "/api/export": api_export,
    "/api/train": api_train,
    "/api/train/start": api_train_start,
    "/api/train/log": api_train_log,
    "/api/train/script": api_train_script,
    "/api/train/tree": api_train_tree,
    "/api/predict": api_predict,
    "/api/library": api_library,
    "/api/library/open": api_library_open,
    "/api/library/export": api_library_export,
    "/api/library/delete": api_library_delete,
    "/api/library/rename": api_library_rename,
    "/api/model": api_model,
    "/api/simulate/form": api_simulate_form,
    "/api/simulate/case": api_simulate_case,
    "/api/simulate/fill": api_simulate_fill,
    "/api/simulate/sweep": api_simulate_sweep,
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

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This is a local tool; nothing here should be embedded elsewhere.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _drain(self, remaining: int) -> None:
        """Read and discard a bounded part of the request body."""
        while remaining > 0:
            chunk = self.rfile.read(min(DRAIN_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

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
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/api/meta":
            self._send_json(200, api_meta({}))
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if handler is None:
            self._send_json(404, {"error": f"no endpoint at {path}"})
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
            self._send_json(200, handler(payload))
        except ApiError as error:
            self._send_json(error.status, {"error": error.message})
        except Exception as error:  # noqa: BLE001 - the UI must not die on one bad request
            traceback.print_exc()
            self._send_json(500, {"error": f"unexpected failure: {error}"})


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False,
          verbose: bool = False, library: str | None = None) -> int:
    global LIBRARY

    Handler.quiet = not verbose
    if library:
        LIBRARY = Store(library)
    httpd = create_server(host, port)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{httpd.server_address[1]}/"

    print(f"FillerAI UI on {url}")
    print(f"  library: {LIBRARY.root}")
    if host not in ("127.0.0.1", "localhost"):
        print("  note: this is bound beyond localhost and has no authentication.")
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
    return 0
