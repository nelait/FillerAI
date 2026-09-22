"""The integration API: a trained model, over HTTP, for somebody else's app.

Everything under ``/api`` is the FillerAI UI talking to its own server. It is
shaped for that: one POST per button, a session cookie, a CSRF header, a
model held in memory under a handle the browser was given a moment ago. None
of that is usable by a claims system that wants to ask *what should go in
these fields* from a different machine, in a different language, next
Tuesday.

So this is a second, separate surface with different promises:

* **Versioned.** Everything is under ``/v1``. The UI's ``/api`` may change
  with the UI; this may not, without the number changing with it.
* **Addressed by library id, not by handle.** ``/v1/models/mdl-...`` is the
  model saved in the library, which is still there after a restart. The UI's
  ``model_id`` is a cache token that is not.
* **Bearer tokens only.** The session cookie is not read here at all - see
  :mod:`fillerai.tokens`. A surface that honoured the cookie would be a
  surface any page in the user's browser could drive.
* **GET for reads.** A caller can look at the model list and the field list
  in a browser address bar, which is most of what makes an API explorable.

The work itself is the same work the UI does: this module holds no modelling
of its own, and that is the point of it existing rather than the endpoints
being bolted onto ``server.py``.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

from .. import __version__
from ..store import StoreError
from ..tokens import ApiToken
from ..train.evaluate import suggest_seed_fields
from ..train.model import ACCEPT_ABOVE, AutofillModel

#: The one version this build speaks. A second one would be a second table.
VERSION = "v1"
PREFIX = f"/{VERSION}/"

#: Records one ``/batch`` call may carry. Enough for a nightly run over a
#: day's queue, bounded so one request cannot hold the server for a minute.
MAX_BATCH = 500
#: Fields one request may claim to have observed already.
MAX_OBSERVED = 2000
#: Models one listing returns. A library with more than this has a problem
#: the integration API is not the place to solve.
MAX_LISTED = 200

#: Models kept loaded between calls, keyed by whose library and which entry.
#: An integration asks the same model over and over - once per keystroke, in
#: the honest case - and reading a few hundred kilobytes of JSON each time
#: would make that unusable. Entries are immutable once written (training
#: again writes a new one), so a loaded model cannot go stale; only a deleted
#: one can linger, and it is evicted when it is noticed.
MAX_CACHED = 8
_LOADED: "OrderedDict[tuple[str, str], AutofillModel]" = OrderedDict()


class RestError(Exception):
    """A refusal with a machine-readable code beside the sentence.

    The UI's :class:`~fillerai.web.server.ApiError` carries a message meant
    for a person to read. This one is read by somebody else's program too, so
    it carries a short stable code as well: the sentence can be improved
    without breaking a caller that branches on the reason.
    """

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass
class Caller:
    """Who is asking, and which library they are asking about.

    Built by the server, which is the part that knows about accounts and
    databases. Everything here takes one of these rather than reaching for
    module state, so every endpoint can be called directly from a test.
    """

    store: Any
    token: ApiToken | None = None

    @property
    def key(self) -> str:
        """A cache key for this caller's library."""
        return str(getattr(self.store, "owner", "") or getattr(self.store, "root", ""))

    def may_use(self, entry_id: str) -> bool:
        """A token pinned to one model may only ever be asked about it."""
        scope = self.token.model_id if self.token else None
        return scope is None or scope == entry_id


@dataclass
class Response:
    status: int
    body: dict[str, Any]
    headers: list[tuple[str, str]] = dc_field(default_factory=list)


# ----------------------------------------------------------------------
# loading a model out of the library
# ----------------------------------------------------------------------


def load(caller: Caller, entry_id: str) -> tuple[AutofillModel, Any]:
    """The model an id names, and its library entry, or a clean refusal."""
    if not caller.may_use(entry_id):
        raise RestError(
            "this token is issued for a different model", status=403,
            code="out_of_scope")

    store = caller.store
    try:
        if not store.has(entry_id):
            raise RestError(f"there is no model called {entry_id!r}",
                            status=404, code="not_found")
        entry = store.get(entry_id)
    except StoreError as error:
        raise RestError(str(error), status=404, code="not_found") from error
    if entry.kind != "model":
        raise RestError(
            f"{entry_id!r} is a {entry.kind}, not a model", status=400,
            code="not_a_model")

    key = (caller.key, entry_id)
    model = _LOADED.get(key)
    if model is None:
        try:
            model = store.load_model(entry_id)
        except StoreError as error:
            raise RestError(str(error), status=404, code="not_found") from error
        _LOADED[key] = model
        while len(_LOADED) > MAX_CACHED:
            _LOADED.popitem(last=False)
    else:
        _LOADED.move_to_end(key)
    return model, entry


def forget(entry_id: str | None = None) -> None:
    """Drop loaded models, so a deleted or replaced entry is not served on.

    Called by the library endpoints that remove things. Without an id it
    clears the lot, which is what the tests and a closing database want.
    """
    if entry_id is None:
        _LOADED.clear()
        return
    for key in [k for k in _LOADED if k[1] == entry_id]:
        _LOADED.pop(key, None)


# ----------------------------------------------------------------------
# reading a request
# ----------------------------------------------------------------------


def _observed(body: dict[str, Any], model: AutofillModel,
              key: str = "observed") -> dict[str, Any]:
    """What the caller says is already filled in, checked against the form.

    An unknown field name is refused rather than ignored. Silently dropping
    it is how an integration ends up believing it sent the policy number for
    a month: the whole value of this call is in what the model was told.
    """
    given = body.get(key) or {}
    if not isinstance(given, dict):
        raise RestError(f"{key!r} must be an object of field names to values",
                        code="bad_observed")
    if len(given) > MAX_OBSERVED:
        raise RestError(f"that is more than {MAX_OBSERVED} fields", status=413,
                        code="too_large")
    unknown = sorted(k for k in given if k not in model.profiles)
    if unknown:
        raise RestError(
            f"this model has no field called {unknown[0]!r}"
            + (f" (and {len(unknown) - 1} other unknown)" if len(unknown) > 1 else ""),
            code="unknown_field")
    return given


def _threshold(body: dict[str, Any]) -> float:
    value = body.get("threshold", ACCEPT_ABOVE)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise RestError("threshold must be a number between 0 and 1",
                        code="bad_threshold") from None
    if not 0.0 <= value <= 1.0:
        raise RestError("threshold must be between 0 and 1", code="bad_threshold")
    return value


# ----------------------------------------------------------------------
# what a model looks like from outside
# ----------------------------------------------------------------------


def _model_card(model: AutofillModel, entry: Any) -> dict[str, Any]:
    """The short description that goes in a listing."""
    return {
        "id": entry.id,
        "name": entry.name,
        "form": model.schema.name or "form",
        "algorithm": model.algorithm,
        "fields": len(model.targets()),
        "trained_on": model.trained_on,
        "held_out": model.held_out,
        "rules": len(model.derivations),
        "model_version": model.model_version,
        "created": entry.created,
    }


def _fields(model: AutofillModel) -> list[dict[str, Any]]:
    """Every field the model will answer about, as a form needs to see it.

    The model's own report says what it can do with a field; the schema says
    what the field *is* - its label, whether it is required, what may go in
    it. A client drawing or binding a form needs both, and asking for them
    separately would only mean every client joining the two itself.
    """
    described = {f.name: f for f in model.schema.fields}
    rows = []
    for row in model.field_report():
        field = described.get(row["name"])
        out = {
            "name": row["name"],
            "label": getattr(field, "label", "") or row["name"],
            "semantic_type": row["semantic_type"],
            "screen": row["screen"],
            "group": row["group"],
            "kind": row["kind"],
            "how": row["how"],
            "from": row["from"],
            "strength": row["strength"],
            "format": row["format"],
            "fill_rate": row["fill_rate"],
        }
        if field is not None:
            out["control"] = field.control
            out["data_type"] = field.data_type
            out["required"] = bool(field.constraints.required)
            out["read_only"] = bool(field.constraints.read_only)
            out["options"] = [o.to_dict() for o in field.options]
            out["placeholder"] = field.placeholder or ""
        rows.append(out)
    return rows


def _suggestions(model: AutofillModel, observed: dict[str, Any],
                 threshold: float, only: list[str] | None) -> list[dict[str, Any]]:
    predictions = model.predict(observed)
    if only is not None:
        predictions = {k: v for k, v in predictions.items() if k in set(only)}
    out = []
    for prediction in predictions.values():
        row = prediction.to_dict()
        # The caller's actual question is "do I put this in the box or not",
        # and answering it here means one rule about the threshold instead of
        # one per client.
        row["accepted"] = bool(prediction.known and prediction.confidence >= threshold)
        out.append(row)
    return out


# ----------------------------------------------------------------------
# the endpoints
# ----------------------------------------------------------------------


def health(caller: Caller | None, params: dict[str, str],
           body: dict[str, Any]) -> dict[str, Any]:
    """Reachable without a token: is this a FillerAI, and does it want one?"""
    return {
        "service": "fillerai",
        "version": __version__,
        "api": VERSION,
        "ok": True,
    }


def models(caller: Caller, params: dict[str, str],
           body: dict[str, Any]) -> dict[str, Any]:
    """Every model in this token's library that it is allowed to ask about."""
    store = caller.store
    entries = [e for e in store.list(kind="model", limit=MAX_LISTED)
               if caller.may_use(e.id)]
    out = []
    for entry in entries:
        # Meta and lineage, never the payload: a library of forty models must
        # not read forty models off disk to answer a listing. The form's name
        # lives on the schema this model descends from, which is the one
        # thing a caller choosing between models actually needs.
        meta = dict(entry.meta or {})
        form = ""
        for ancestor in store.lineage(entry.id)[:-1]:
            if ancestor.kind == "schema":
                form = ancestor.name
        out.append({
            "id": entry.id,
            "name": entry.name,
            "form": form or entry.name,
            "algorithm": meta.get("algorithm"),
            "trained_on": meta.get("trained_on"),
            "rules": meta.get("rules"),
            "created": entry.created,
        })
    return {"models": out, "count": len(out)}


def model(caller: Caller, params: dict[str, str],
          body: dict[str, Any]) -> dict[str, Any]:
    """One model in full: what it is, what it knows, what to ask for first."""
    loaded, entry = load(caller, params["id"])
    return {
        "model": _model_card(loaded, entry),
        "threshold": ACCEPT_ABOVE,
        "ask_first": suggest_seed_fields(loaded, 3),
        "screens": [s.to_dict() for s in loaded.schema.screens],
        "fields": _fields(loaded),
    }


def suggest(caller: Caller, params: dict[str, str],
            body: dict[str, Any]) -> dict[str, Any]:
    """What the model would put in the fields that are still empty.

    The reasoning comes back with each answer - the confidence, what it was
    derived from, the runners-up - because an agent-facing form wants to show
    *why* before a person accepts a hundred values they did not type.
    """
    loaded, entry = load(caller, params["id"])
    observed = _observed(body, loaded)
    threshold = _threshold(body)

    only = body.get("fields")
    if only is not None:
        if not isinstance(only, list):
            raise RestError("fields must be a list of field names",
                            code="bad_fields")
        only = [str(f) for f in only]
        unknown = [f for f in only if f not in loaded.profiles]
        if unknown:
            raise RestError(f"this model has no field called {unknown[0]!r}",
                            code="unknown_field")

    rows = _suggestions(loaded, observed, threshold, only)
    return {
        "model_id": entry.id,
        "threshold": threshold,
        "given": {k: str(v) for k, v in observed.items()},
        "suggestions": rows,
        "values": {r["field"]: r["value"] for r in rows if r["accepted"]},
        "offered": sum(1 for r in rows if r["accepted"]),
        "considered": len(rows),
    }


def fill(caller: Caller, params: dict[str, str],
         body: dict[str, Any]) -> dict[str, Any]:
    """The record as the model would hand it back, confident answers only.

    ``suggest`` for a client that wants to reason about each field;
    ``fill`` for one that wants the finished record and will show the person
    what changed.
    """
    loaded, entry = load(caller, params["id"])
    observed = _observed(body, loaded)
    threshold = _threshold(body)
    record = loaded.filled(observed, threshold=threshold)
    added = [k for k in record if k not in observed]
    return {
        "model_id": entry.id,
        "threshold": threshold,
        "record": record,
        "filled": added,
        "offered": len(added),
    }


def batch(caller: Caller, params: dict[str, str],
          body: dict[str, Any]) -> dict[str, Any]:
    """The same as ``fill``, over many partial records in one call.

    A queue of yesterday's intake is the case this exists for: one HTTP round
    trip and one model load, rather than five hundred of each.
    """
    loaded, entry = load(caller, params["id"])
    threshold = _threshold(body)
    records = body.get("records")
    if not isinstance(records, list) or not records:
        raise RestError("records must be a non-empty list of partial records",
                        code="bad_records")
    if len(records) > MAX_BATCH:
        raise RestError(f"that is more than {MAX_BATCH} records for one call",
                        status=413, code="too_large")
    if not all(isinstance(r, dict) for r in records):
        raise RestError("every record must be an object of field names to values",
                        code="bad_records")

    results, offered = [], 0
    for index, given in enumerate(records):
        observed = _observed({"observed": given}, loaded)
        completed = loaded.filled(observed, threshold=threshold)
        added = [k for k in completed if k not in observed]
        offered += len(added)
        results.append({"index": index, "record": completed, "filled": added})
    return {
        "model_id": entry.id,
        "threshold": threshold,
        "count": len(results),
        "offered": offered,
        "results": results,
    }


# ----------------------------------------------------------------------
# routing
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    method: str
    pattern: "re.Pattern[str]"
    handler: Callable[..., dict[str, Any]]
    #: False only for the handful of things a caller may ask before it has
    #: proved who it is.
    needs_token: bool = True


ENDPOINTS: list[Endpoint] = [
    Endpoint("GET", re.compile(r"^/v1/health$"), health, needs_token=False),
    Endpoint("GET", re.compile(r"^/v1/models$"), models),
    Endpoint("GET", re.compile(r"^/v1/models/(?P<id>[A-Za-z0-9._-]{1,80})$"), model),
    Endpoint("POST", re.compile(r"^/v1/models/(?P<id>[A-Za-z0-9._-]{1,80})/suggest$"),
             suggest),
    Endpoint("POST", re.compile(r"^/v1/models/(?P<id>[A-Za-z0-9._-]{1,80})/fill$"),
             fill),
    Endpoint("POST", re.compile(r"^/v1/models/(?P<id>[A-Za-z0-9._-]{1,80})/batch$"),
             batch),
]


def match(method: str, path: str) -> tuple[Endpoint, dict[str, str]] | None:
    """The endpoint a request lands on, or None.

    A path that matches under another method is reported as that method
    rather than as a 404, so a caller that sent GET where POST was meant is
    told the truth instead of being sent looking for a typo.
    """
    wrong_method = False
    for endpoint in ENDPOINTS:
        found = endpoint.pattern.match(path)
        if found is None:
            continue
        if endpoint.method != method.upper():
            wrong_method = True
            continue
        return endpoint, found.groupdict()
    if wrong_method:
        raise RestError(f"{method} is not how you call {path}", status=405,
                        code="method_not_allowed")
    return None


def bearer(header: str) -> str:
    """The credential in an ``Authorization`` header, or the empty string."""
    raw = str(header or "").strip()
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()
