"""Phase 4: play the model against a sample form and see what it saves.

The first three phases read a form, invent data for it, and learn to finish
it. This one puts them in front of someone: the form is drawn from its own
schema, a few fields are typed into it, the model fills what it can, and the
run is costed against filling the same form by hand.

Typical use::

    from fillerai import extract_html, generate, train
    from fillerai.simulate import layout, run, sweep
    from fillerai.train import suggest_seed_fields

    schema = extract_html("examples/claims_intake.html")
    model = train(schema, generate(schema, count=400, seed=1).records)

    # a form the model has never seen, standing in for the next customer
    case = generate(schema, count=1, seed=99).records[0]
    seeds = suggest_seed_fields(model, 3)

    result = run(model, {n: case[n] for n in seeds}, case=case)
    print(result.headline())

The saving is modelled, not measured, and :mod:`.effort` holds every
assumption it rests on in one place.
"""

from __future__ import annotations

from .effort import DEFAULT_EFFORT, Effort, spell_out
from .form import Control, Layout, Page, Section, layout
from .run import (
    FILLED,
    SUGGESTED,
    TYPED,
    YOURS,
    Cell,
    Run,
    Savings,
    Score,
    Sweep,
    run,
    sweep,
)

__all__ = [
    "Cell",
    "Control",
    "DEFAULT_EFFORT",
    "Effort",
    "FILLED",
    "Layout",
    "Page",
    "Run",
    "Savings",
    "Score",
    "Section",
    "SUGGESTED",
    "Sweep",
    "TYPED",
    "YOURS",
    "layout",
    "run",
    "spell_out",
    "sweep",
]
