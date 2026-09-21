"""Phase 3: learn to finish a form from the first few fields of it.

The dataset from phase 2 is the training data and the field schema is what
says which column means what, so nothing here needs to guess at the shape of
the form it is looking at.

Typical use::

    from fillerai import extract_html, generate
    from fillerai.train import train, suggest_seed_fields

    schema = extract_html("examples/claims_intake.html")
    records = generate(schema, count=500, seed=42).records

    model = train(schema, records, TrainOptions(algorithm="forest"))
    print(suggest_seed_fields(model, 3))

    predictions = model.predict({"home_postal_code": "78701"})
    for name, prediction in predictions.items():
        if prediction.confidence > 0.7:
            print(name, prediction.value, prediction.because)

How it learns is a choice. :mod:`fillerai.train.algos` holds the engines -
conditional tables, a decision tree, a forest of them, a nearest-record
search, naive Bayes - and everything around the choice is shared: the same
profiling, the same verified rules, the same quarter of the records held
back, the same calibration measured against them. So the algorithms are
comparable rather than merely both available, and a confidence of 0.8 means
the same thing whichever one produced it.

Every stage of a run can be watched while it happens
(:class:`fillerai.train.trace.Trace`) and written out as a script that
reproduces it (:mod:`fillerai.train.script`).

Still no dependencies, and still nothing that leaves the machine: the model
is counted off the records in front of it and saved as a JSON file you can
read.
"""

from __future__ import annotations

from . import algos, script
from .algos import Algorithm, Engine, Guess
from .algos.combine import Combiner
from .evaluate import FieldScore, Report, evaluate, suggest_seed_fields
from .features import Profile
from .model import (
    ACCEPT_ABOVE,
    MODEL_VERSION,
    AutofillModel,
    Calibration,
    Prediction,
    TrainOptions,
    split_records,
    train,
)
from .trace import Trace

__all__ = [
    "ACCEPT_ABOVE",
    "Algorithm",
    "AutofillModel",
    "Calibration",
    "FieldScore",
    "MODEL_VERSION",
    "Engine",
    "Guess",
    "Prediction",
    "Profile",
    "Report",
    "Trace",
    "Combiner",
    "TrainOptions",
    "algos",
    "evaluate",
    "script",
    "split_records",
    "suggest_seed_fields",
    "train",
]
