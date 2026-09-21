"""The autofill model: fit it on records, ask it to finish a form.

The shape of the problem is not classification. Nobody wants one number out
of this; they want forty-odd fields filled from the five an agent has
already typed, and they want to know which of the forty to trust. So the
model is a per-field predictor with a shared, measured notion of confidence.

Three things can answer for a field, in this order:

1. **A rule.** Verified against the training data in :mod:`.derive`. If the
   age is the date of birth's arithmetic, say so exactly. Rules are
   arithmetic, not statistics, so they sit above the choice of algorithm
   rather than inside it.
2. **The engine.** The selectable part, and the only selectable part:
   conditional tables, a decision tree, a forest, a nearest-record search or
   naive Bayes, all in :mod:`.algos`. Given the fields an agent has typed, it
   says what the rest of the form probably holds.
3. **What the field usually says.** The marginal distribution, always mixed
   in at a low weight so that thin evidence backs off to the common answer
   instead of committing to a bucket of two rows.

And a fourth outcome, which is a real answer rather than a failure: the
field is a claim number, it is different in every record, and the honest
prediction is none at all.

**What stays shared, and why that is the interesting part.** Profiling the
columns, verifying the rules, holding a quarter of the records back,
calibrating the confidence against them and scoring the result are the same
code whichever engine is chosen. That is what makes the algorithms
comparable: a confidence of 0.8 is measured the same way and means the same
thing whether a forest or a naive Bayes produced it, so changing the
dropdown changes the model and not the yardstick. It is also what keeps
naive Bayes usable at all - its raw scores are famously overconfident, and
the calibration step is measuring, not trusting, them.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..schema import FormSchema
from . import algos, associate, derive, features
from .algos import Engine, combine
from .algos.base import MARGINAL_WEIGHT  # noqa: F401 - kept importable
from .algos.combine import Combiner
from .algos.statistical import StatisticalEngine, links_from
from .associate import Link
from .derive import Derivation
from .features import Profile
from .trace import Trace, resolve

# Bumped from "1.0" when the engine became selectable and moved into its own
# key. A 1.x model still loads: it was necessarily fitted with conditional
# tables, so its links are read straight into that engine.
MODEL_VERSION = "2.0"

# How many runners-up to keep. Enough for a UI to offer a choice, few enough
# that a prediction stays a prediction.
MAX_ALTERNATIVES = 4

# With no other field pointing at it, a prediction is just the commonest
# value. That is a real answer for a country box that says "US" in every
# record, and noise for a first name whose commonest value covers 2% of
# them. Below this the model says it does not know, which is true and more
# useful than a name picked out of a hat.
USUAL_FLOOR = 0.25

# Predictions at or above this are offered without a warning. Deliberately
# strict: on a claims form a wrong value that looks filled in costs more
# than an empty box, and a coin-flip honestly reported as 0.55 is still a
# coin flip. Callers who would rather review more suggestions can lower it.
ACCEPT_ABOVE = 0.7


@dataclass
class Prediction:
    """One field's answer, and the reason for it."""

    field: str
    value: str | None = None
    confidence: float = 0.0  # calibrated: how often answers like this are right
    score: float = 0.0  # raw, before calibration
    basis: str = "none"  # "rule" | "learned" | "usual" | "none"
    because: list[str] = dc_field(default_factory=list)
    alternatives: list[tuple[str, float]] = dc_field(default_factory=list)
    # For a field nothing can predict, the shape its values take is the one
    # useful thing left to say.
    format: str | None = None

    @property
    def known(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field": self.field,
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "score": round(self.score, 4),
            "basis": self.basis,
        }
        if self.because:
            out["because"] = self.because
        if self.alternatives:
            out["alternatives"] = [[v, round(p, 4)] for v, p in self.alternatives]
        if self.format:
            out["format"] = self.format
        return out


@dataclass
class TrainOptions:
    """Knobs for a training run."""

    # Which engine learns the relationships. One of :func:`algos.names`.
    algorithm: str = algos.DEFAULT
    # Share of the records held back, used only to measure. Confidence that
    # was fitted on the same rows it describes is not confidence.
    holdout: float = 0.25
    seed: int | None = None
    min_lambda: float = associate.MIN_LAMBDA
    max_predictors: int = associate.MAX_PREDICTORS
    # Per-algorithm settings, keyed by the knobs each one declares. Kept as a
    # loose dict rather than a field per knob so adding an algorithm does not
    # mean editing this class.
    tuning: dict[str, Any] = dc_field(default_factory=dict)
    # Verified rules are not part of the algorithm choice, but turning them
    # off is the only way to see what an engine is doing on its own - with
    # them on, an engine gets no credit for the fields a rule already
    # answers exactly.
    use_rules: bool = True
    # Fit the vote weights instead of using the hand-picked ones. Needs enough
    # held-out records to spare some, and the fitted weights are kept only if
    # they do at least as well - see :mod:`.algos.combine`.
    learn_weights: bool = True
    # Somewhere to watch the run from. ``None`` means nobody is watching.
    trace: Trace | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "holdout": self.holdout,
            "seed": self.seed,
            "tuning": dict(self.tuning),
            "use_rules": self.use_rules,
            "learn_weights": self.learn_weights,
        }


# ----------------------------------------------------------------------
# calibration
# ----------------------------------------------------------------------

BIN_COUNT = 10
# Pulls a sparse bin back towards the raw score it is supposed to correct,
# so three lucky rows cannot certify a whole band as perfect.
CALIBRATION_PRIOR = 5.0
MIN_CALIBRATION_ROWS = 20


@dataclass
class Calibration:
    """How often predictions of a given raw score actually turned out right.

    A raw score is a probability under the model's own assumptions, which are
    wrong in the usual ways. This measures the gap on records the model never
    saw, so the number a user reads means what it says: at 0.8, four out of
    five are right.
    """

    bins: list[tuple[int, int]] = dc_field(default_factory=list)  # (attempts, hits)
    rates: list[float] = dc_field(default_factory=list)
    fitted: bool = False

    def apply(self, raw: float) -> float:
        if not self.fitted or not self.rates:
            return raw
        index = min(BIN_COUNT - 1, max(0, int(raw * BIN_COUNT)))
        return self.rates[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitted": self.fitted,
            "bins": [[a, h] for a, h in self.bins],
            "rates": [round(r, 4) for r in self.rates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Calibration":
        return cls(
            bins=[(int(a), int(h)) for a, h in (data.get("bins") or [])],
            rates=[float(r) for r in (data.get("rates") or [])],
            fitted=bool(data.get("fitted", False)),
        )


def _isotonic(rates: list[float], weights: list[float]) -> list[float]:
    """Force the curve to never go down, by pooling adjacent violators.

    Confidence that dips in the middle is an artefact of thin bins, not a
    finding: higher raw scores really are more often right. Pooling the
    offending neighbours into their shared weighted mean is the standard
    repair, and it leaves every bin that was already in order untouched.
    """
    # Each block carries its weighted total, its weight, and how many of the
    # original bins it now stands for, so the curve can be laid back out.
    blocks = [[rate * weight, weight, 1] for rate, weight in zip(rates, weights)]
    index = 0
    while index < len(blocks) - 1:
        left, right = blocks[index], blocks[index + 1]
        if left[0] / left[1] <= right[0] / right[1]:
            index += 1
            continue
        blocks.pop(index + 1)
        left[0] += right[0]
        left[1] += right[1]
        left[2] += right[2]
        # Merging can break the order with the block before it, so step back.
        if index:
            index -= 1

    out: list[float] = []
    for total, weight, span in blocks:
        out.extend([total / weight] * span)
    return out


# ----------------------------------------------------------------------
# the model
# ----------------------------------------------------------------------


@dataclass
class AutofillModel:
    """Everything learned about one form, ready to finish a record."""

    schema: FormSchema
    profiles: dict[str, Profile] = dc_field(default_factory=dict)
    engine: Engine = dc_field(default_factory=lambda: StatisticalEngine({}))
    derivations: dict[str, Derivation] = dc_field(default_factory=dict)
    calibration: Calibration = dc_field(default_factory=Calibration)
    #: How much each of the engine's votes deserves to be believed. Unfitted
    #: by default, which is the hand-picked arithmetic.
    combiner: Combiner = dc_field(default_factory=Combiner)
    trained_on: int = 0
    held_out: int = 0
    model_version: str = MODEL_VERSION
    algorithm: str = algos.DEFAULT
    #: What the run was asked for, so a saved model says how to reproduce it.
    settings: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        # The engine does the voting, so it is the engine that has to hold the
        # combiner. Keeping the assignment here means a model built any way at
        # all - fitted, loaded, or by hand in a test - is consistent.
        self.engine.combiner = self.combiner

    def set_combiner(self, combiner: Combiner) -> None:
        self.combiner = combiner
        self.engine.combiner = combiner

    @property
    def links(self) -> dict[str, list[Link]]:
        """The conditional tables, when the engine is the one that has them.

        Kept because it is the one piece of a fitted model that code outside
        this package reasonably reaches for, and because every model written
        before the engine was selectable had it.
        """
        return getattr(self.engine, "links", {})

    # -- prediction --------------------------------------------------------

    def targets(self) -> list[str]:
        """Fields the model is willing to be asked about, in schema order."""
        return [f.name for f in self.schema.fields
                if not f.constraints.read_only and f.name in self.profiles]

    def predict(self, observed: dict[str, Any]) -> dict[str, Prediction]:
        """Predict every field the caller has not already filled in."""
        known = {k: features.normalise(v) for k, v in observed.items()}
        known = {k: v for k, v in known.items() if v}
        return {
            name: self.predict_field(name, known)
            for name in self.targets() if name not in known
        }

    def filled(self, observed: dict[str, Any],
               threshold: float = ACCEPT_ABOVE) -> dict[str, str]:
        """The record as the model would hand it back, confident answers only."""
        out = {k: features.normalise(v) for k, v in observed.items()}
        for name, prediction in self.predict(observed).items():
            if prediction.known and prediction.confidence >= threshold:
                out[name] = prediction.value or ""
        return out

    def predict_field(self, name: str, known: dict[str, str]) -> Prediction:
        profile = self.profiles.get(name)
        if profile is None:
            return Prediction(field=name)

        rule = self.derivations.get(name)
        if rule is not None:
            value = rule.apply(known)
            if value is not None:
                return Prediction(
                    field=name, value=value, score=rule.accuracy,
                    confidence=self.calibration.apply(rule.accuracy),
                    basis="rule",
                    because=[_explain_rule(rule)],
                )

        if profile.kind == "open":
            # An open field has no set of candidates to choose from. Saying
            # so is the right answer, not a gap for an engine to paper over
            # with the mode, so no engine is even asked.
            return Prediction(field=name, format=profile.format,
                              because=_no_evidence_reason(profile))

        guess = self.engine.guess(name, known, profile)
        if not guess.known:
            return Prediction(field=name, format=profile.format,
                              because=_no_evidence_reason(profile))

        value, raw = guess.value, guess.score
        if not guess.used_evidence and raw < USUAL_FLOOR:
            return Prediction(field=name, format=profile.format,
                              because=[f"no usual value - the commonest is "
                                       f"{value!r}, in {raw * 100:.0f}% of records"])
        basis = "learned" if guess.used_evidence else "usual"
        # The reason has to name the value actually chosen. Reading it off
        # the marginal separately drifts apart from the answer wherever two
        # values tie, and a reason that contradicts the value beside it is
        # worse than no reason at all.
        because = guess.because or [f"no field seen so far narrows this; {value} is "
                                    f"the usual value, in {raw * 100:.0f}% of records"]
        return Prediction(
            field=name, value=value, score=raw,
            confidence=self.calibration.apply(raw),
            basis=basis, because=because,
            alternatives=list(guess.alternatives[:MAX_ALTERNATIVES]),
            format=profile.format,
        )

    # -- reporting ---------------------------------------------------------

    def field_report(self) -> list[dict[str, Any]]:
        """One row per field: what the model can do with it, and from what."""
        rows = []
        for name in self.targets():
            profile = self.profiles[name]
            rule = self.derivations.get(name)
            learned = self.engine.sources(name) if profile.kind != "open" else []
            modal = profile.modal() or ("", 0.0)
            if rule is not None:
                how, sources, strength = "rule", list(rule.inputs), rule.accuracy
            elif learned and self.engine.strength(name) > 0:
                how = "learned"
                sources = learned
                strength = self.engine.strength(name)
            elif profile.kind in ("constant", "enumerable") and modal[1] >= USUAL_FLOOR:
                how, sources, strength = "usual", [], modal[1]
            else:
                # Nothing points at it and it has no habit worth the name, so
                # it is the agent's to type. Saying which fields those are is
                # half of what this report is for.
                how, sources, strength = "you", [], 0.0
            rows.append({
                "name": name,
                "semantic_type": profile.semantic_type,
                "screen": profile.screen,
                "group": profile.group,
                "kind": profile.kind,
                "fill_rate": round(profile.fill_rate, 3),
                "distinct": profile.distinct,
                "how": how,
                "from": sources,
                "strength": round(strength, 3),
                "format": profile.format,
                "examples": [v for v, _ in profile.values[:3]],
                # How this particular engine would put it. A forest asks
                # about fields; a nearest-record search matches on them; a
                # rule computes from them. The report says which.
                "detail": self.engine.explain(name) if how == "learned" else "",
            })
        return rows

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": MODEL_VERSION,
            "algorithm": self.algorithm,
            "schema": self.schema.to_dict(),
            "trained_on": self.trained_on,
            "held_out": self.held_out,
            "settings": dict(self.settings),
            "profiles": [p.to_dict() for p in self.profiles.values()],
            "engine": self.engine.to_dict(),
            "derivations": [d.to_dict() for d in self.derivations.values()],
            "calibration": self.calibration.to_dict(),
            "combiner": self.combiner.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutofillModel":
        """Rebuild a model, including one saved before engines were selectable.

        A 1.x file keeps its links at the top level and names no algorithm,
        because there was only one. Reading those straight into the
        conditional-table engine is the whole of the migration - the numbers
        did not change, only where they are written down.
        """
        version = str(data.get("model_version", MODEL_VERSION))
        major = version.split(".")[0]
        if major not in ("1", MODEL_VERSION.split(".")[0]):
            raise ValueError(
                f"model_version {version!r} is not compatible with "
                f"{MODEL_VERSION!r} supported by this build of FillerAI"
            )

        if major == "1":
            algorithm = "statistical"
            engine: Engine = StatisticalEngine(links_from(data.get("links") or []))
        else:
            algorithm = str(data.get("algorithm", algos.DEFAULT))
            engine = algos.get(algorithm).load(data.get("engine") or {})

        return cls(
            schema=FormSchema.from_dict(data["schema"]),
            profiles={p["name"]: Profile.from_dict(p) for p in data.get("profiles", [])},
            engine=engine,
            algorithm=algorithm,
            settings=dict(data.get("settings") or {}),
            derivations={
                d["target"]: Derivation.from_dict(d) for d in data.get("derivations", [])
            },
            calibration=Calibration.from_dict(data.get("calibration") or {}),
            combiner=Combiner.from_dict(data.get("combiner") or {}),
            trained_on=int(data.get("trained_on", 0)),
            held_out=int(data.get("held_out", 0)),
            model_version=MODEL_VERSION,
        )

    @classmethod
    def from_json(cls, text: str) -> "AutofillModel":
        return cls.from_dict(json.loads(text))


def _explain_rule(rule: Derivation) -> str:
    inputs = ", ".join(rule.inputs)
    wording = {
        "copy": f"always the same as {inputs}",
        "join": f"{inputs} joined as {rule.params.get('template', '')}",
        "initial": f"the first letter of {inputs}",
        "email": f"{rule.params.get('template', '')}@{rule.params.get('domain', '')} "
                 f"built from {inputs}",
        "age": f"years between {inputs} and {rule.params.get('as_of', 'today')}",
    }.get(rule.kind, f"derived from {inputs}")
    return f"rule: {wording} ({rule.accuracy * 100:.0f}% of {rule.support} records)"


def _no_evidence_reason(profile: Profile) -> list[str]:
    if profile.kind == "open":
        shape = f", shaped like {profile.format}" if profile.format else ""
        return [f"different in every record{shape} - this one is yours to type"]
    if not profile.filled:
        return ["never filled in the training data"]
    return ["nothing learned about this field"]


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def train(schema: FormSchema, records: list[dict[str, Any]],
          options: TrainOptions | None = None) -> AutofillModel:
    """Fit an autofill model for ``schema`` on ``records``.

    The stages are the same whichever algorithm is chosen, and they are in
    this order for a reason: the records are split before anything is
    counted, so the rows that measure the confidence are rows nothing was
    fitted on.
    """
    options = options or TrainOptions()
    trace = resolve(options.trace)
    if not records:
        raise ValueError("training needs at least one record")

    algorithm = algos.get(options.algorithm)
    trace.expect(7)
    trace.step(f"{algorithm.label}: {len(records)} record(s), "
               f"{len(schema.fields)} field(s) on the form")

    fit_rows, holdout_rows = split_records(records, options)
    trace.detail(
        f"  {len(fit_rows)} to learn from, {len(holdout_rows)} held back to measure"
        if holdout_rows else
        f"  too few records to hold any back, so confidence will not be claimed"
    )

    trace.step("profiling every writable column")
    columns = {
        f.name: features.column(fit_rows, f.name)
        for f in schema.fields if not f.constraints.read_only
    }
    profiles = {
        f.name: features.profile_field(f, columns[f.name])
        for f in schema.fields if not f.constraints.read_only
    }
    kinds: dict[str, int] = {}
    for profile in profiles.values():
        kinds[profile.kind] = kinds.get(profile.kind, 0) + 1
    trace.detail("  " + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
    open_fields = [p.name for p in profiles.values() if p.kind == "open"]
    if open_fields:
        trace.detail(f"  {len(open_fields)} field(s) are different in every record "
                     f"and nothing can predict them: {', '.join(open_fields[:4])}"
                     + (", ..." if len(open_fields) > 4 else ""))

    if options.use_rules:
        trace.step("proposing rules from the field meanings, and checking each "
                   "against the records")
        derivations = derive.learn_derivations(schema, fit_rows, profiles, columns)
        trace.done(f"{len(derivations)} rule(s) held up")
        for rule in list(derivations.values())[:6]:
            trace.detail(f"  {rule.target} = {_explain_rule(rule)}")
    else:
        trace.step("rules turned off for this run")
        derivations = {}

    context = algos.FitContext(
        schema=schema, records=fit_rows, columns=columns, profiles=profiles,
        trace=trace, options=options,
    )
    engine = algorithm.fit(context)

    model = AutofillModel(
        schema=schema, profiles=profiles, engine=engine, derivations=derivations,
        trained_on=len(fit_rows), held_out=len(holdout_rows),
        algorithm=algorithm.name, settings=options.to_dict(),
    )

    tune_rows, measure_rows = split_holdout(holdout_rows, options)
    if tune_rows:
        trace.step(f"learning how loudly each voter should speak, on "
                   f"{len(tune_rows)} record(s) set aside for it")
        combiner = _fit_combiner(model, tune_rows, options.seed)
        # Installed either way. A declined combiner is unfitted, so it hands
        # back the hand-picked weight untouched - but it carries the two
        # numbers that say so, and those are worth saving with the model.
        model.set_combiner(combiner)
        if combiner.fitted:
            trace.done(f"fitted vote weights kept: loss {combiner.loss:.4f} against "
                       f"{combiner.baseline_loss:.4f} hand-picked, on records "
                       f"neither was fitted on")
        elif combiner.votes:
            trace.done(f"fitted vote weights declined: loss {combiner.loss:.4f} "
                       f"against {combiner.baseline_loss:.4f} hand-picked, so the "
                       f"hand-picked ones stay")
        else:
            trace.detail("  too few votes to fit weights from; "
                         "the hand-picked ones stay")
        for line in combiner.explain()[1:]:
            trace.detail("  " + line)
    elif options.learn_weights and holdout_rows:
        trace.step(f"too few records held back ({len(holdout_rows)}) to spare any "
                   f"for fitting vote weights; the hand-picked ones stay")

    trace.step(
        f"calibrating confidence on {len(measure_rows)} unseen record(s)"
        if measure_rows else "no records held back, so nothing to calibrate against"
    )
    model.calibration = _calibrate(model, measure_rows, options.seed)
    if model.calibration.fitted:
        attempts = sum(a for a, _ in model.calibration.bins)
        trace.done(f"measured on {attempts} prediction(s) the model had never seen")
    else:
        trace.warn("not enough held-out predictions to calibrate; "
                   "the raw scores are passed through as they are")

    report = model.field_report()
    fillable = sum(1 for row in report if row["how"] != "you")
    trace.step(f"done: {fillable} of {len(report)} field(s) can be answered, "
               f"{len(report) - fillable} are the agent's to type")
    trace.finish()
    return model


def split_records(records: list[dict[str, Any]],
           options: TrainOptions) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate the rows used to learn from the rows used to measure.

    With too few records to spare any, everything is used to fit and the
    model simply says it is uncalibrated, which is more useful than a
    confidence measured on four rows.
    """
    if options.holdout <= 0 or len(records) < MIN_CALIBRATION_ROWS:
        return list(records), []
    shuffled = list(records)
    random.Random(options.seed if options.seed is not None else 0).shuffle(shuffled)
    cut = max(1, int(len(shuffled) * min(options.holdout, 0.5)))
    return shuffled[cut:], shuffled[:cut]


# Below this many held-out records, none of them are spared for fitting vote
# weights: the confidence curve is the thing that must not be starved, and a
# curve measured on a hundred records is already thin.
COMBINER_MIN_HOLDOUT = 200

# How much top-1 accuracy the fitted weights are allowed to give up for a
# better-ordered set of scores. Measured on a few thousand answers, accuracy
# moves by about this much on noise alone, so demanding none at all would veto
# genuine improvements to the thing the gate is actually reading.
COMBINER_ACCURACY_SLACK = 0.005

# And never more than this many, however large the holdout. Six parameters
# settle long before it, and every row taken is a row the calibration does not
# get - at 20,000 records this leaves the curve 4,800 of its 5,000.
COMBINER_TUNE_ROWS = 200


def split_holdout(holdout: list[dict[str, Any]],
                  options: TrainOptions) -> tuple[list[dict[str, Any]],
                                                  list[dict[str, Any]]]:
    """Separate the rows that tune the vote weights from the rows that measure.

    These have to be different rows. Weights chosen on the records the
    confidence curve is then measured against would make that curve describe
    rows the weights were picked for, which is exactly what holding records
    back is for.
    """
    if not options.learn_weights or len(holdout) < COMBINER_MIN_HOLDOUT:
        return [], list(holdout)
    cut = min(COMBINER_TUNE_ROWS, len(holdout) // 3)
    return holdout[:cut], holdout[cut:]


# How many fields the simulated agent has typed, when measuring. Sweeping the
# range is deliberate: confidence has to mean the same thing on the second
# field of a form as on the thirtieth.
_EVIDENCE_SIZES = (1, 2, 3, 5, 8)


def _evidence_walk(model: AutofillModel, rows: list[dict[str, Any]],
                   seed: int | None):
    """Every (typed fields, record) pair the measuring passes work from.

    One generator for all of them, so the confidence curve, the vote weights
    and any comparison between them are measured against the same idea of what
    an agent has typed - a handful of fields, swept across sizes.
    """
    rng = random.Random(seed if seed is not None else 0)
    names = model.targets()
    for record in rows:
        present = [n for n in names if features.normalise(record.get(n))]
        if len(present) < 2:
            continue
        for size in _EVIDENCE_SIZES:
            if size >= len(present):
                break
            given = rng.sample(present, size)
            yield {n: features.normalise(record[n]) for n in given}, record


def _calibrate(model: AutofillModel, holdout: list[dict[str, Any]],
               seed: int | None) -> Calibration:
    if len(holdout) < 3:
        return Calibration()

    attempts = [0] * BIN_COUNT
    hits = [0] * BIN_COUNT
    names = model.targets()

    for known, record in _evidence_walk(model, holdout, seed):
        for name in names:
            if name in known:
                continue
            truth = features.normalise(record.get(name))
            if not truth:
                continue
            prediction = model.predict_field(name, known)
            if not prediction.known:
                continue
            index = min(BIN_COUNT - 1, max(0, int(prediction.score * BIN_COUNT)))
            attempts[index] += 1
            if prediction.value == truth:
                hits[index] += 1

    if sum(attempts) < MIN_CALIBRATION_ROWS:
        return Calibration()

    midpoints = [(i + 0.5) / BIN_COUNT for i in range(BIN_COUNT)]
    rates = [
        (hit + CALIBRATION_PRIOR * mid) / (attempt + CALIBRATION_PRIOR)
        for attempt, hit, mid in zip(attempts, hits, midpoints)
    ]
    weights = [float(a) + CALIBRATION_PRIOR for a in attempts]
    return Calibration(
        bins=list(zip(attempts, hits)),
        rates=[round(r, 4) for r in _isotonic(rates, weights)],
        fitted=True,
    )


# ----------------------------------------------------------------------
# vote weights
# ----------------------------------------------------------------------


def _collect_votes(model: AutofillModel, rows: list[dict[str, Any]],
                   seed: int | None) -> list[tuple[tuple[float, ...], bool]]:
    """Every individual vote the engine casts on ``rows``, and whether it was right.

    Fields a rule answers are skipped, because no ballot is held for them, and
    so are open fields, because no engine is asked about them. What is left is
    exactly the population the vote weights govern.
    """
    votes: list[tuple[tuple[float, ...], bool]] = []
    names = model.targets()
    with combine.recording():
        for known, record in _evidence_walk(model, rows, seed):
            for name in names:
                if name in known or name in model.derivations:
                    continue
                profile = model.profiles.get(name)
                if profile is None or profile.kind == "open":
                    continue
                truth = features.normalise(record.get(name))
                if not truth:
                    continue
                guess = model.engine.guess(name, known, profile)
                for vote, top in guess.votes:
                    votes.append((vote, top == truth))
    return votes


def _voted_quality(model: AutofillModel, rows: list[dict[str, Any]],
                   seed: int | None) -> tuple[float, float]:
    """``(log loss, accuracy)`` of the ballots the vote weights govern.

    Only the engine is asked, so rules and open fields are out of it: they come
    out the same whatever the weights are, and including them would dilute the
    difference being measured.

    The loss comes first because it is what the gate reads. Weights cannot
    change a ballot with one voter at all - the result is normalised by the
    weight cast - and where voters do compete, the first thing better weights
    change is how far apart the candidates end up rather than which one wins.
    The log loss of the true value measures that; top-1 accuracy is mostly
    blind to it.
    """
    attempts = hits = 0
    total_loss = 0.0
    names = model.targets()
    for known, record in _evidence_walk(model, rows, seed):
        for name in names:
            if name in known or name in model.derivations:
                continue
            profile = model.profiles.get(name)
            if profile is None or profile.kind == "open":
                continue
            truth = features.normalise(record.get(name))
            if not truth:
                continue
            guess = model.engine.guess(name, known, profile)
            if not guess.known:
                continue
            attempts += 1
            hits += guess.value == truth
            total_loss -= math.log(max(guess.distribution.get(truth, 0.0), 1e-9))
    if not attempts:
        return 0.0, 0.0
    return total_loss / attempts, hits / attempts


def _fit_combiner(model: AutofillModel, rows: list[dict[str, Any]],
                  seed: int | None) -> Combiner:
    """Fit vote weights on half of ``rows``, then make them earn their place.

    The half that decides is not the half that fitted, and neither is the
    holdout the confidence is measured on. Returning an unfitted combiner
    means the hand-picked weights stay - and it still carries the two numbers,
    because "measured and not better" is a result, not a silence.
    """
    cut = max(1, len(rows) // 2)
    learn, check = rows[:cut], rows[cut:]
    candidate = combine.fit(_collect_votes(model, learn, seed), seed)
    if not candidate.fitted or not check:
        return Combiner()

    was = model.combiner
    try:
        model.set_combiner(Combiner())  # the hand-picked arithmetic
        base_loss, base_hits = _voted_quality(model, check, seed)
        model.set_combiner(candidate)
        loss, hits = _voted_quality(model, check, seed)
    finally:
        model.set_combiner(was)

    candidate.loss, candidate.baseline_loss = loss, base_loss
    candidate.accuracy, candidate.baseline_accuracy = hits, base_hits
    if loss > base_loss or hits < base_hits - COMBINER_ACCURACY_SLACK:
        # Measured and not better, so the hand-picked weights stay. The numbers
        # come back anyway: a fit that was tried and declined is a result.
        return Combiner(votes=candidate.votes, loss=loss, baseline_loss=base_loss,
                        accuracy=hits, baseline_accuracy=base_hits)
    return candidate
