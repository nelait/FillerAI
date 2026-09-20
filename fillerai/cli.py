"""Command line interface: ``python -m fillerai``."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

from . import __version__, extract_html, extract_spec
from .generate.dataset import Options, coherence_report, generate, validate
from .schema import FormSchema
from .simulate.effort import DEFAULT_EFFORT, spell_out
from .simulate.run import run as simulate_form, sweep as simulate_forms
from .store import DEFAULT_DIRNAME, HOME_VARIABLE, KINDS, Store, StoreError
from .train import algos, script as script_writer
from .train.evaluate import evaluate, suggest_seed_fields
from .train.model import (
    ACCEPT_ABOVE,
    AutofillModel,
    TrainOptions,
    split_records,
    train,
)
from .train.trace import Trace


def _load_schema(path: Path) -> FormSchema:
    if path.suffix.lower() in (".html", ".htm"):
        return extract_html(path)
    return extract_spec(path)


def _load_records(path: Path) -> list[dict]:
    """Read a dataset back in, whichever way ``generate`` wrote it out.

    A CSV has lost the difference between ``false`` and the string
    ``"false"`` by the time it is on disk. That is fine: the model
    normalises every value the same way before counting it, so a record
    read from CSV trains exactly like the JSON it came from.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    if suffix == ".ndjson":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("records", [])
    if not isinstance(data, list):
        raise ValueError(f"{path} does not hold a list of records")
    return data


def _load_model(path: Path) -> AutofillModel:
    return AutofillModel.from_json(path.read_text(encoding="utf-8"))


def _library(args: argparse.Namespace) -> Store:
    return Store(args.library) if getattr(args, "library", None) else Store.default()


def _tuning(pairs: list[str] | None) -> dict[str, object]:
    """``--set trees=16`` into ``{"trees": 16}``, numbers where they look it."""
    out: dict[str, object] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set wants key=value, not {pair!r}")
        key, value = pair.split("=", 1)
        text = value.strip()
        if text.lower() in ("true", "false"):
            out[key.strip()] = text.lower() == "true"
            continue
        try:
            out[key.strip()] = int(text)
        except ValueError:
            try:
                out[key.strip()] = float(text)
            except ValueError:
                out[key.strip()] = text
    return out


def _write(text: str, out: str | None) -> None:
    if out and out != "-":
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    source = Path(args.source)
    schema = _load_schema(source)
    if args.save:
        store = _library(args)
        kind = "html" if source.suffix.lower() in (".html", ".htm") else "spec"
        parent = store.save_source(
            source.read_text(encoding="utf-8"), kind=kind, name=source.name)
        entry = store.save_schema(schema, parent=parent.id)
        print(f"library: {entry.id}  (from {parent.id})", file=sys.stderr)
    _write(schema.to_json(), args.out)
    if not args.out or args.out == "-":
        return 0
    low = [f for f in schema.fields if f.confidence < args.review_below]
    print(
        f"{len(schema.fields)} fields across {len(schema.screens) or 1} screen(s); "
        f"{len(low)} below confidence {args.review_below}",
        file=sys.stderr,
    )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    schema = _load_schema(Path(args.schema))
    dataset = generate(schema, Options(
        count=args.count,
        seed=args.seed,
        blank_rate=args.blank_rate,
        safe_identifiers=not args.realistic_identifiers,
        include_persona=args.include_persona,
    ))

    if args.check:
        problems = validate(schema, dataset.records) + coherence_report(schema, dataset.records)
        if problems:
            for problem in problems[:25]:
                print(f"FAIL {problem}", file=sys.stderr)
            if len(problems) > 25:
                print(f"... and {len(problems) - 25} more", file=sys.stderr)
            return 1
        print(f"checked {len(dataset.records)} records: no problems", file=sys.stderr)

    if args.save:
        store = _library(args)
        parent = args.from_schema or store.save_schema(schema).id
        entry = store.save_dataset(
            dataset.records, parent=parent,
            name=f"{schema.name or 'form'}: {len(dataset.records)} records",
            meta={"seed": args.seed, "blank_rate": args.blank_rate},
        )
        print(f"library: {entry.id}  (from {parent})", file=sys.stderr)

    _write(dataset.render(args.format), args.out)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    schema = _load_schema(Path(args.source))
    screens = {s.id: s.title for s in schema.screens}
    print(f"{schema.name}  ({len(schema.fields)} fields, schema {schema.schema_version})")

    current = object()
    for field in schema.fields:
        if field.screen != current:
            current = field.screen
            title = screens.get(field.screen or "") or field.screen or "(no screen)"
            print(f"\n  == {title} ==")
        flags = []
        if field.constraints.required:
            flags.append("required")
        if field.group:
            flags.append(f"group={field.group}")
        if field.options:
            flags.append(f"{len(field.options)} options")
        marker = " " if field.confidence >= args.review_below else "?"
        print(
            f"  {marker} {field.name:<28} {field.semantic_type:<20} "
            f"{field.confidence:.2f}  {' '.join(flags)}"
        )
        if args.evidence and field.evidence:
            for line in field.evidence:
                print(f"      - {line}")

    low = [f for f in schema.fields if f.confidence < args.review_below]
    if low:
        print(f"\n  {len(low)} field(s) marked '?' are worth a human look.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    return serve(host=args.host, port=args.port, open_browser=args.open,
                 verbose=args.verbose, library=args.library)


def cmd_check(args: argparse.Namespace) -> int:
    schema = _load_schema(Path(args.schema))
    records = json.loads(Path(args.records).read_text(encoding="utf-8"))
    if isinstance(records, dict):
        records = records.get("records", [])
    problems = validate(schema, records) + coherence_report(schema, records)
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"{len(records)} records, {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0



def _train_options(args: argparse.Namespace, trace: Trace | None = None) -> TrainOptions:
    return TrainOptions(
        algorithm=args.algorithm,
        holdout=args.holdout,
        seed=args.seed,
        tuning=_tuning(getattr(args, "set", None)),
        use_rules=not args.no_rules,
        trace=trace,
    )


def cmd_train(args: argparse.Namespace) -> int:
    schema = _load_schema(Path(args.schema))
    records = _load_records(Path(args.records))
    if not records:
        print("that dataset has no records in it", file=sys.stderr)
        return 1

    # ``--compare`` with no names means every algorithm, so an empty list is
    # a request rather than an absence.
    if args.compare is not None:
        return _compare(schema, records, args)

    # ``--verbose`` echoes every stage as it happens. On a wide form the fit
    # takes a few seconds, and a few seconds of silence is indistinguishable
    # from a hang.
    trace = Trace(echo=args.verbose)
    options = _train_options(args, trace)
    model = train(schema, records, options)
    seeds = args.seeds.split(",") if args.seeds else suggest_seed_fields(model, args.ask)

    if args.script:
        _write(script_writer.script(options, schema, schema_path=args.schema,
                                    records_path=args.records,
                                    model_path=args.out or "form.model.json",
                                    seeds=seeds),
               args.script)

    print(f"{schema.name}: {algos.get(args.algorithm).label} learned from "
          f"{model.trained_on} records"
          + (f", {model.held_out} held back to measure" if model.held_out else ""),
          file=sys.stderr)
    _print_field_report(model)
    print(f"\n  ask the agent for: {', '.join(seeds) or '(nothing useful found)'}",
          file=sys.stderr)
    summary = model.engine.summary()
    if summary:
        print("  " + ", ".join(f"{k}: {v}" for k, v in summary.items()), file=sys.stderr)

    if args.tree:
        drawn = getattr(model.engine, "drawn", lambda *_: [])(args.tree)
        if drawn:
            print(f"\n  the tree for {args.tree}\n", file=sys.stderr)
            for line in drawn:
                print(f"    {line}", file=sys.stderr)
        else:
            print(f"\n  {args.algorithm} has no tree to draw for {args.tree!r}",
                  file=sys.stderr)

    if args.evaluate:
        report = evaluate(model, _load_records(Path(args.evaluate)),
                          seeds=seeds, threshold=args.threshold)
        print(f"  on {args.evaluate}: {report.headline()}", file=sys.stderr)

    if args.save:
        store = _library(args)
        parent = args.from_dataset
        if not parent:
            # Nothing said where these records came from, so the library
            # records the schema and the dataset too. A model whose lineage
            # stops at itself is the one thing the library exists to prevent.
            schema_entry = store.save_schema(schema)
            parent = store.save_dataset(
                records, parent=schema_entry.id,
                name=f"{schema.name or 'form'}: {len(records)} records").id
        entry = store.save_model(
            model, parent=parent,
            name=f"{schema.name or 'form'}: {args.algorithm}",
            meta={"seeds": seeds},
        )
        print(f"library: {entry.id}  (from {parent})", file=sys.stderr)

    if args.out:
        _write(model.to_json(), args.out)
    return 0


def _compare(schema: FormSchema, records: list[dict], args: argparse.Namespace) -> int:
    """Fit every algorithm on the same records and put them side by side.

    Which algorithm suits a form is a question about that form, not one with
    a general answer, so the useful thing the tool can do is stop guessing and
    measure. Every run here gets the same records, the same split, the same
    seed and the same held-out rows, so the columns really are comparable.
    """
    chosen = list(args.compare) or algos.names()
    unknown = [name for name in chosen if name not in algos.names()]
    if unknown:
        print(f"no algorithm called {unknown[0]!r}; try one of "
              f"{', '.join(algos.names())}", file=sys.stderr)
        return 1
    print(f"{schema.name}: {len(records)} record(s), "
          f"{len(chosen)} algorithm(s), same split for each\n")
    print(f"  {'algorithm':<14}{'fills':>7}{'right':>7}{'offers':>8}{'rules':>7}"
          f"{'fit':>8}  asks for")
    rows = []
    for name in chosen:
        args.algorithm = name
        options = _train_options(args)
        started = time.monotonic()
        model = train(schema, records, options)
        elapsed = time.monotonic() - started
        # The same split the fit used, recomputed: those are the rows no
        # algorithm in the table has seen.
        _, holdout = split_records(records, options)
        seeds = args.seeds.split(",") if args.seeds else suggest_seed_fields(model, args.ask)
        report = evaluate(model, holdout or records, seeds=seeds,
                          threshold=args.threshold)
        rows.append((name, report, model, elapsed, seeds))
        print(f"  {name:<14}{report.coverage * 100:6.0f}%{report.accepted_accuracy * 100:6.0f}%"
              f"{report.offered * 100:7.0f}%{len(model.derivations):7d}{elapsed:7.1f}s  "
              f"{', '.join(seeds[:2])}")

    best = max(rows, key=lambda row: (row[1].coverage * row[1].accepted_accuracy,
                                      -row[3]))
    print(f"\n  fills   = share of the remaining fields it filled confidently")
    print(f"  right   = how often those filled values were correct")
    print(f"  offers  = share it had any answer for, confident or not")
    print(f"\n  best on this form: {best[0]} - {best[1].headline()}")
    print(f"  measured on the {best[1].records} record(s) held back from every run.")
    return 0


def cmd_algorithms(args: argparse.Namespace) -> int:
    """What can be selected, and what each one actually does."""
    for algorithm in algos.all_algorithms():
        mark = "*" if algorithm.name == algos.DEFAULT else " "
        print(f"{mark} {algorithm.name:<12} {algorithm.label}")
        print(f"    {algorithm.blurb}")
        if args.recipe:
            for index, line in enumerate(algorithm.recipe, start=1):
                print(f"      {index}. {line}")
        if algorithm.knobs:
            knobs = ", ".join(f"{k}={default}" for k, _l, _t, default, _lo, _hi
                              in algorithm.knobs)
            print(f"      --set {knobs}")
        print()
    print(f"  * is the default. Pick one with --algorithm, tune it with --set.")
    return 0


# ----------------------------------------------------------------------
# the library
# ----------------------------------------------------------------------


def cmd_library(args: argparse.Namespace) -> int:
    store = _library(args)
    try:
        return _LIBRARY[args.action](store, args)
    except StoreError as error:
        print(str(error), file=sys.stderr)
        return 1


def _library_list(store: Store, args: argparse.Namespace) -> int:
    entries = store.list(kind=args.kind, parent=args.under, limit=args.limit)
    if not entries:
        print(f"the library at {store.root} is empty", file=sys.stderr)
        return 0
    print(f"{store.root}\n")
    for entry in entries:
        detail = ", ".join(f"{k} {v}" for k, v in entry.meta.items()
                           if v not in (None, "", []))
        print(f"  {entry.id}  {entry.kind:<8}{entry.when():<17} {entry.name}")
        if detail:
            print(f"      {detail[:100]}")
    totals = store.totals()
    print(f"\n  " + ", ".join(f"{count} {kind}(s)" for kind, count in totals.items())
          + f", {store.size() // 1024} KB")
    return 0


def _library_show(store: Store, args: argparse.Namespace) -> int:
    entry = store.get(args.id)
    print(f"{entry.id}  {entry.kind}\n  {entry.name}\n  made {entry.when()}, "
          f"{entry.bytes // 1024} KB")
    for key, value in entry.meta.items():
        print(f"  {key}: {value}")

    lineage = store.lineage(entry.id)[:-1]
    if lineage:
        print("\n  made from")
        for step, ancestor in enumerate(lineage):
            print(f"    {'  ' * step}{ancestor.id}  {ancestor.kind:<8} {ancestor.name}")
    children = store.children(entry.id)
    if children:
        print("\n  used to make")
        for child in children:
            print(f"    {child.id}  {child.kind:<8} {child.name}")
    return 0


def _library_export(store: Store, args: argparse.Namespace) -> int:
    payload = store.payload(args.id)
    _write(json.dumps(payload, indent=2, ensure_ascii=False), args.out)
    return 0


def _library_delete(store: Store, args: argparse.Namespace) -> int:
    removed = store.delete(args.id, cascade=args.cascade)
    print(f"removed {len(removed)}: {', '.join(removed)}", file=sys.stderr)
    return 0


def _library_prune(store: Store, args: argparse.Namespace) -> int:
    removed = store.prune(keep=args.keep)
    print(f"removed {len(removed)} old entr(ies), keeping {args.keep} of each kind",
          file=sys.stderr)
    return 0


_LIBRARY = {
    "list": _library_list,
    "show": _library_show,
    "export": _library_export,
    "delete": _library_delete,
    "prune": _library_prune,
}


_HOW_WORDS = {
    "rule": "a rule",
    "learned": "other fields",
    "usual": "the usual value",
    "you": "you",
}


def _print_field_report(model: AutofillModel) -> None:
    """Field by field, what the model will lean on when it is asked."""
    current = object()
    for row in model.field_report():
        if row["screen"] != current:
            current = row["screen"]
            print(f"\n  == {current or '(no screen)'} ==", file=sys.stderr)
        if row["how"] == "you":
            shape = f", shaped like {row['format']}" if row["format"] else ""
            detail = f"different in every record{shape}"
        elif row["from"]:
            detail = row.get("detail") or ("from " + ", ".join(row["from"][:3]))
        else:
            example = (row["examples"] or [""])[0]
            detail = f"nearly always {example}" if example else ""
        print(f"    {row['name']:<28} {_HOW_WORDS[row['how']]:<16} "
              f"{row['strength']:.2f}  {detail}", file=sys.stderr)


def cmd_predict(args: argparse.Namespace) -> int:
    model = _load_model(Path(args.model))
    observed: dict[str, str] = {}
    for pair in args.set or []:
        if "=" not in pair:
            print(f"--set wants field=value, not {pair!r}", file=sys.stderr)
            return 1
        name, value = pair.split("=", 1)
        observed[name.strip()] = value.strip()

    unknown = [name for name in observed if name not in model.profiles]
    if unknown:
        print(f"this model has no field called {', '.join(sorted(unknown))}",
              file=sys.stderr)
        return 1

    predictions = model.predict(observed)
    if args.format == "json":
        _write(json.dumps({
            "given": observed,
            "predictions": [p.to_dict() for p in predictions.values()],
        }, indent=2, ensure_ascii=False), args.out)
        return 0

    record = dict(observed)
    for name, prediction in predictions.items():
        if prediction.known and prediction.confidence >= args.threshold:
            record[name] = prediction.value or ""
    if args.format == "record":
        _write(json.dumps(record, indent=2, ensure_ascii=False), args.out)
        return 0

    for name in model.targets():
        if name in observed:
            print(f"  {name:<28} {observed[name]:<28} given")
            continue
        prediction = predictions[name]
        if not prediction.known:
            hint = f"looks like {prediction.format}" if prediction.format else "yours to type"
            print(f"  {name:<28} {'-':<28} {hint}")
            continue
        marker = " " if prediction.confidence >= args.threshold else "?"
        print(f" {marker}{name:<28} {str(prediction.value):<28} "
              f"{prediction.confidence:.2f}  {'; '.join(prediction.because)}")
    filled = sum(1 for p in predictions.values()
                 if p.known and p.confidence >= args.threshold)
    print(f"\n  {len(observed)} given, {filled} filled at {args.threshold} "
          f"or better, {len(predictions) - filled} left for you",
          file=sys.stderr)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Play a model against forms it has never seen, and cost the result."""
    model = _load_model(Path(args.model))

    if args.records:
        cases = _load_records(Path(args.records))
        where = args.records
    else:
        # Fresh records from the model's own schema. Not the training set: a
        # form the model has already learned would flatter every number here.
        cases = generate(model.schema, Options(count=args.forms, seed=args.seed)).records
        where = f"{len(cases)} fresh form(s)"
    if not cases:
        print("there are no forms to simulate", file=sys.stderr)
        return 1

    seeds = args.seeds.split(",") if args.seeds else suggest_seed_fields(model, args.ask)
    unknown = [s for s in seeds if s not in model.profiles]
    if unknown:
        print(f"this model has no field called {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1

    result = simulate_forms(model, cases, seeds, threshold=args.threshold)
    print(f"{model.schema.name}: {where}, typing {', '.join(seeds) or '(nothing)'}")
    print(f"  {result.headline()}")
    print(f"  {result.keystrokes_by_hand - result.keystrokes_now} fewer keystrokes "
          f"across {result.cases} form(s), {spell_out(result.seconds_saved)} in total")
    print("\n  counted as: " + "; ".join(DEFAULT_EFFORT.assumptions()))

    if args.show:
        _show_one(model, cases[0], seeds, args.threshold)
    if args.out:
        _write(json.dumps(result.to_dict(), indent=2), args.out)
    return 0


def _show_one(model: AutofillModel, case: dict, seeds: list[str],
              threshold: float) -> None:
    """One form in full, so the totals above have something behind them."""
    typed = {name: case[name] for name in seeds
             if str(case.get(name) or "").strip()}
    result = simulate_form(model, typed, case=case, threshold=threshold)
    print(f"\n  one form in full ({result.headline()})\n")
    words = {"typed": "you typed", "filled": "filled",
             "suggested": "held back", "yours": "yours"}
    for cell in result.cells:
        mark = ""
        if cell.verdict == "right":
            mark = "ok"
        elif cell.verdict == "wrong":
            mark = f"WRONG, the form says {cell.truth}"
        shown = cell.value or "-"
        print(f"    {cell.name:<28} {shown[:26]:<28} {words[cell.source]:<10} "
              f"{cell.confidence:.2f}  {mark}")


def cmd_evaluate(args: argparse.Namespace) -> int:
    model = _load_model(Path(args.model))
    records = _load_records(Path(args.records))
    seeds = args.seeds.split(",") if args.seeds else suggest_seed_fields(model, args.ask)
    report = evaluate(model, records, seeds=seeds, threshold=args.threshold)

    print(f"  seeds: {', '.join(report.seeds)}")
    print(f"  {report.headline()}")
    print("\n  does the confidence mean anything?")
    for band in report.reliability:
        if not band["count"]:
            continue
        print(f"    said {band['from']:.2f}-{band['to']:.2f} -> "
              f"right {band['accuracy'] * 100:5.1f}% of {band['count']}")

    worst = sorted((f for f in report.fields if f.attempted),
                   key=lambda f: (f.accuracy, f.name))
    if worst:
        print("\n  weakest fields")
        for score in worst[:8]:
            print(f"    {score.name:<28} {score.correct:>4}/{score.attempted:<4} "
                  f"{score.accuracy * 100:5.1f}%  mean confidence {score.mean_confidence:.2f}")
    if args.out:
        _write(json.dumps(report.to_dict(), indent=2), args.out)
    return 0


# ----------------------------------------------------------------------


def _add_library_flags(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--save", action="store_true", help=help_text)
    parser.add_argument("--library", metavar="PATH",
                        help=f"where the library lives (default: ./{DEFAULT_DIRNAME}, "
                             f"or ${HOME_VARIABLE})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fillerai",
        description="Read a web form, generate sample data for it, "
                "learn to fill it, then watch it being filled.",
    )
    parser.add_argument("--version", action="version", version=f"fillerai {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common_review = dict(type=float, default=0.7,
                         help="confidence below which a field is flagged for review")

    extract = subparsers.add_parser(
        "extract", help="read a form and write its field schema")
    extract.add_argument("source", help="an .html page or a .json field spec")
    extract.add_argument("-o", "--out", help="output path, '-' for stdout")
    extract.add_argument("--review-below", **common_review)
    _add_library_flags(extract, "keep the source and the schema in the library")
    extract.set_defaults(func=cmd_extract)

    gen = subparsers.add_parser("generate", help="generate records from a schema")
    gen.add_argument("schema", help="a schema .json, field spec, or .html page")
    gen.add_argument("-n", "--count", type=int, default=10, help="how many records")
    gen.add_argument("-o", "--out", help="output path, '-' for stdout")
    gen.add_argument("-f", "--format", choices=("json", "ndjson", "csv"), default="json")
    gen.add_argument("--seed", type=int, help="make the run reproducible")
    gen.add_argument("--blank-rate", type=float, default=0.12,
                     help="share of optional fields left empty (0 fills everything)")
    gen.add_argument("--realistic-identifiers", action="store_true",
                     help="use full real-world ranges for SSNs, phones and cards "
                          "instead of the reserved non-issuable ones")
    gen.add_argument("--include-persona", action="store_true",
                     help="attach the entity behind each record, for debugging")
    gen.add_argument("--check", action="store_true",
                     help="validate the records before writing them")
    _add_library_flags(gen, "keep the dataset in the library")
    gen.add_argument("--from-schema", metavar="ID",
                     help="the library schema these records are for")
    gen.set_defaults(func=cmd_generate)

    inspect = subparsers.add_parser(
        "inspect", help="show what was inferred, screen by screen")
    inspect.add_argument("source", help="an .html page, field spec, or schema")
    inspect.add_argument("--evidence", action="store_true", help="show why each type was chosen")
    inspect.add_argument("--review-below", **common_review)
    inspect.set_defaults(func=cmd_inspect)

    serve = subparsers.add_parser("serve", help="open the FillerAI UI in a browser")
    serve.add_argument("-p", "--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address; the default keeps the UI on this machine")
    serve.add_argument("--open", action="store_true", help="open a browser window")
    serve.add_argument("-v", "--verbose", action="store_true", help="log each request")
    serve.add_argument("--library", metavar="PATH",
                       help=f"where the UI keeps what it produces "
                            f"(default: ./{DEFAULT_DIRNAME}, or ${HOME_VARIABLE})")
    serve.set_defaults(func=cmd_serve)

    trainer = subparsers.add_parser(
        "train", help="learn to autofill a form from a dataset")
    trainer.add_argument("schema", help="a schema .json, field spec, or .html page")
    trainer.add_argument("records", help="a .json, .ndjson or .csv dataset")
    trainer.add_argument("-o", "--out", help="where to write the model")
    trainer.add_argument("-a", "--algorithm", default=algos.DEFAULT,
                         choices=algos.names(),
                         help="how to learn the relationships; "
                              "'fillerai algorithms' explains each one")
    trainer.add_argument("--set", action="append", metavar="KEY=VALUE",
                         help="a setting for the chosen algorithm; repeatable")
    trainer.add_argument("--no-rules", action="store_true",
                         help="skip the verified rules, to see what the "
                              "algorithm manages on its own")
    trainer.add_argument("--compare", nargs="*", metavar="ALGORITHM",
                         help="fit every algorithm on the same records and "
                              "print them side by side; name some to narrow it")
    trainer.add_argument("--tree", metavar="FIELD",
                         help="draw the tree grown for one field")
    trainer.add_argument("--script", metavar="PATH",
                         help="write a runnable script that reproduces this run")
    trainer.add_argument("-v", "--verbose", action="store_true",
                         help="print each stage of the fit as it happens")
    trainer.add_argument("--holdout", type=float, default=0.25,
                         help="share of records kept back to measure confidence on")
    trainer.add_argument("--seed", type=int, help="make the run reproducible")
    trainer.add_argument("--ask", type=int, default=3,
                         help="how many fields to suggest asking the agent for")
    trainer.add_argument("--seeds", help="use these fields instead, comma separated")
    trainer.add_argument("--evaluate", help="a second dataset to score the model on")
    trainer.add_argument("--threshold", type=float, default=ACCEPT_ABOVE,
                         help="confidence at which a prediction is offered")
    _add_library_flags(trainer, "keep the model in the library")
    trainer.add_argument("--from-dataset", metavar="ID",
                         help="the library dataset these records came from")
    trainer.set_defaults(func=cmd_train)

    lister = subparsers.add_parser(
        "algorithms", help="what can be selected, and what each one does")
    lister.add_argument("--recipe", action="store_true",
                        help="show each algorithm's steps in order")
    lister.set_defaults(func=cmd_algorithms)

    predict = subparsers.add_parser(
        "predict", help="fill in the rest of a form from a few fields")
    predict.add_argument("model", help="a model written by train")
    predict.add_argument("--set", action="append", metavar="FIELD=VALUE",
                         help="a field the agent has already typed; repeatable")
    predict.add_argument("-f", "--format", choices=("text", "json", "record"),
                         default="text",
                         help="'record' gives the filled form on its own")
    predict.add_argument("-o", "--out", help="output path, '-' for stdout")
    predict.add_argument("--threshold", type=float, default=ACCEPT_ABOVE,
                         help="confidence at which a prediction is offered")
    predict.set_defaults(func=cmd_predict)

    scorer = subparsers.add_parser(
        "evaluate", help="score a model on records it has not seen")
    scorer.add_argument("model", help="a model written by train")
    scorer.add_argument("records", help="a .json, .ndjson or .csv dataset")
    scorer.add_argument("--ask", type=int, default=3,
                        help="how many seed fields to assume the agent types")
    scorer.add_argument("--seeds", help="use these fields instead, comma separated")
    scorer.add_argument("--threshold", type=float, default=ACCEPT_ABOVE,
                        help="confidence at which a prediction is offered")
    scorer.add_argument("-o", "--out", help="write the full report as JSON")
    scorer.set_defaults(func=cmd_evaluate)

    player = subparsers.add_parser(
        "simulate", help="play a model against forms it has never seen")
    player.add_argument("model", help="a model written by train")
    player.add_argument("-n", "--forms", type=int, default=25,
                        help="how many fresh forms to work through")
    player.add_argument("--records",
                        help="use this dataset as the forms instead of generating them")
    player.add_argument("--ask", type=int, default=3,
                        help="how many fields the agent is assumed to type")
    player.add_argument("--seeds", help="use these fields instead, comma separated")
    player.add_argument("--threshold", type=float, default=ACCEPT_ABOVE,
                        help="confidence at which a prediction is filled in")
    player.add_argument("--seed", type=int,
                        help="make the generated forms reproducible")
    player.add_argument("--show", action="store_true",
                        help="print the first form field by field")
    player.add_argument("-o", "--out", help="write the full report as JSON")
    player.set_defaults(func=cmd_simulate)

    check = subparsers.add_parser(
        "check", help="validate an existing dataset against a schema")
    check.add_argument("schema")
    check.add_argument("records", help="a .json dataset produced by generate")
    check.set_defaults(func=cmd_check)

    library = subparsers.add_parser(
        "library", help="browse what past runs produced, and what it came from")
    library.add_argument("--library", metavar="PATH",
                         help=f"where the library lives (default: ./{DEFAULT_DIRNAME}, "
                              f"or ${HOME_VARIABLE})")
    actions = library.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="everything in it, newest first")
    listing.add_argument("-k", "--kind", choices=KINDS, help="only this kind")
    listing.add_argument("--under", metavar="ID", help="only things made from this")
    listing.add_argument("-n", "--limit", type=int, help="at most this many")

    showing = actions.add_parser(
        "show", help="one entry, what it was made from, and what came of it")
    showing.add_argument("id")

    export = actions.add_parser("export", help="write one entry's contents out")
    export.add_argument("id")
    export.add_argument("-o", "--out", help="output path, '-' for stdout")

    remove = actions.add_parser("delete", help="remove one entry")
    remove.add_argument("id")
    remove.add_argument("--cascade", action="store_true",
                        help="also remove everything made from it")

    prune = actions.add_parser("prune", help="drop the oldest entries")
    prune.add_argument("--keep", type=int, default=50,
                       help="how many of each kind to keep")

    library.set_defaults(func=cmd_library)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
