"""FillerAI - read a form, fill a form.

Read a form and work out what each field means; generate as many coherent
fake records as you need, without any real data ever leaving the environment
it lives in; learn from those records how to finish the form from the first
few fields an agent types; then play that model against a form it has never
seen and find out what it actually saved.

Typical use::

    from fillerai import extract_html, generate, train

    schema = extract_html("examples/customer_onboarding.html")
    dataset = generate(schema, count=500, seed=42)

    model = train(schema, dataset.records, algorithm="forest")
    for name, guess in model.predict({"home_postal_code": "78701"}).items():
        if guess.confidence > 0.8:
            print(name, guess.value)

    unseen = generate(schema, count=100, seed=99).records
    print(simulate_many(model, unseen, suggest_seed_fields(model, 3)).headline())
"""

from __future__ import annotations

from pathlib import Path

from .auth import Auth, AuthError, User
from .db import Database, DatabaseError, connect as connect_database
from .dbstore import DatabaseStore, import_store
from .extract import html_form, spec
from .generate.dataset import Dataset, Options, coherence_report, generate as _generate, validate
from .infer import infer, infer_field
from .schema import SCHEMA_VERSION, Constraints, Field, FormSchema, Option, Screen
from .simulate.run import Run, Savings, Score, Sweep, run as simulate, sweep as simulate_many
from .store import Entry, Store
from .train import algos
from .train.evaluate import Report, evaluate, suggest_seed_fields
from .train.model import AutofillModel, Prediction, TrainOptions, train as _train
from .train.trace import Trace

__version__ = "0.6.1"

__all__ = [
    "SCHEMA_VERSION",
    "__version__",
    "Auth",
    "AuthError",
    "AutofillModel",
    "Constraints",
    "Database",
    "DatabaseError",
    "DatabaseStore",
    "Dataset",
    "Entry",
    "Field",
    "FormSchema",
    "Option",
    "Options",
    "Prediction",
    "Report",
    "Run",
    "Savings",
    "Score",
    "Screen",
    "Store",
    "Sweep",
    "User",
    "Trace",
    "TrainOptions",
    "algos",
    "coherence_report",
    "connect_database",
    "evaluate",
    "extract_html",
    "extract_spec",
    "generate",
    "import_store",
    "infer",
    "infer_field",
    "simulate",
    "simulate_many",
    "suggest_seed_fields",
    "train",
    "validate",
]


def extract_html(path: str | Path, name: str | None = None) -> FormSchema:
    """Read an HTML form and return an inferred schema."""
    return infer(html_form.extract_file(path, name=name))


def extract_spec(path: str | Path) -> FormSchema:
    """Read a field specification (or a saved schema) and infer what is missing."""
    return infer(spec.load_file(path))


def generate(schema: FormSchema, count: int = 10, seed: int | None = None,
             blank_rate: float = 0.12, safe_identifiers: bool = True,
             include_persona: bool = False) -> Dataset:
    """Generate ``count`` coherent records for ``schema``."""
    return _generate(schema, Options(
        count=count, seed=seed, blank_rate=blank_rate,
        safe_identifiers=safe_identifiers, include_persona=include_persona,
    ))


def train(schema: FormSchema, records: list[dict[str, object]],
          holdout: float = 0.25, seed: int | None = None,
          algorithm: str = algos.DEFAULT) -> AutofillModel:
    """Learn to finish ``schema``'s form from ``records``.

    ``holdout`` is the share of the records kept back to measure confidence
    on, rather than to learn from. Confidence fitted on the rows it
    describes is not confidence, so the default spends a quarter of the data
    on getting that number honest.

    ``algorithm`` picks the engine: ``"statistical"``, ``"tree"``,
    ``"forest"``, ``"nearest"``, ``"bayes"`` or ``"linear"``. For anything
    beyond the name
    - the per-algorithm settings, a trace to watch it run - build a
    :class:`TrainOptions` and call :func:`fillerai.train.train` directly.
    """
    return _train(schema, records, TrainOptions(
        algorithm=algorithm, holdout=holdout, seed=seed))
