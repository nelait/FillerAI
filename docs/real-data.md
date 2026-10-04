# Real records: testing on them, and training from them

Everything up to the Data step invents its records, because a company that
will not hand over past submissions can still hand over its form. Once real
records do arrive, they are wanted for two jobs:

1. **Testing a model you already have.** A model trained on generated records
   is scored on what was really submitted. This is the number worth quoting:
   the Simulate stage's own sweep only tells you how the model does on the
   world it was taught.
2. **Training a model from scratch on them**, instead of on generated
   records. The relationships only real history carries (an employer's email
   pattern, a claim type that implies a diagnosis) are exactly what the
   generator cannot invent, so this is where the saving moves.

Both go through the same three steps first, in `fillerai/realdata.py`, which
the UI, the CLI and the tests all call.

## Read, map, clean

**Read.** A CSV (comma, semicolon, tab or pipe), a JSON list of objects (or
`{"records": [...]}`), or NDJSON. A byte-order mark, blank lines, short rows
and blank or repeated headers are all handled; every cell comes out as text.

**Map.** Each column is matched to one of the form's fields by name or
label, exactly first and then by near match (`difflib`, cutoff 0.82). A field
is given to one column at most, and read-only fields are never mapped,
because the system fills them, not the person. The guess is shown and can be
changed; a column left out is simply not used, and a field with no column is
blank in every record.

**Clean.** Eight named fixes run in this order. Every one reports how many
cells (or rows) it changed, with examples, and every one can be switched off.
A fix that is off still counts what it *would* have done, so the report can
say "229 rows miss a required field" before anybody decides to drop them.

| Fix | Default | What it does |
|---|---|---|
| `trim` | on | Strips the ends, collapses runs of spaces. Line breaks inside a long answer stay. |
| `blanks` | on | `N/A`, `null`, `none`, `-`, `?`, `#N/A`, `nan` and friends become empty, unless the field offers that word as an option. |
| `case` | on | ALL-CAPS names, streets, cities, companies and job titles become Title Case (word by word; anything with a digit, like `7B`, is left alone). Emails are lower-cased. |
| `options` | on | A value is matched to the field's option by value or label, ignoring case and punctuation: `california` becomes `CA`. Multi-selects are split on `\|`, `;` or `,`. |
| `types` | on | Numbers lose currency signs and separators and are written as the generator writes them (`$1,250.5` becomes `1250.50`; an integer field gets `3` for `3.0`). Dates in about twenty common shapes are rewritten in the field's own format. Yes/no becomes a tick for a checkbox, or `Yes`/`No` for a text box. |
| `invalid` | off | Empties any value the form would still reject: not an option, not a number, out of range, too long, the wrong date format, failing the pattern. |
| `duplicates` | on | Drops a row identical to an earlier one. |
| `incomplete` | off | Drops a row with a required field empty. |

`invalid` and `incomplete` throw information away, which is why they wait to
be asked. Whatever is still wrong after cleaning is listed per field and kind
(`home_state not one of its options in 192 rows, e.g. "PA", "MI"`), so the
usual fix, a wrong column mapping, is easy to spot.

The cleaned records have the shape a generated record has: one key per
fillable field, in the form's order, with dates, numbers, options and yes/no
written the way the generator writes them. That is what lets a model trained
on generated records be scored on real ones, and what lets every later stage
take real records without knowing where they came from. The test suite pins
this: every bundled example, generated, written out as CSV, JSON and NDJSON,
and cleaned back, comes back unchanged.

## No form, only a file

The Source step's **Records (CSV)** tab, and `fillerai extract records.csv`,
read a form off the file's columns. Each column becomes a field named after
it; the values decide only what they can prove (yes/no, a date, a whole or
decimal number, or a short list of repeated choices, and a long free-text
answer). What each field *means* is left to inference, which reads names and
labels better than a pass over values could. Digits with a leading zero, or
always the same long length, stay text: they are ZIP codes and account
numbers, not amounts. The result is an ordinary field spec, kept in the
library as the source, so it can be downloaded, corrected and read back.

## In the UI

**Data step, Upload real records.** Choose or drop the file. The column
mapping and the cleaning report appear side by side, with the cleaned rows
underneath; every change re-runs the clean on the server, so the preview is
exactly what will be kept. **Save cleaned records** puts them in the library
as a dataset under the form's schema, marked `origin: real` with the file
name and what cleaning did, and makes them the records on screen.

The next-step bar then offers **Train now** (a model trained on them carries
the real dataset in its lineage) and, when a model is already loaded, **Test
the current model**.

**Simulate step, Test on real records.** Pick any dataset in the library
(real ones for this model's form are listed first) and press **Test**. Each
record is filled from its own first few fields, the ones the model asks for,
and every answer is scored against what was really there. The card shows how
much of the rest was filled, how often a filled value was right, the time
saved, and the five weakest fields. It warns when the model learned from the
very records it is being tested on, and when some of the model's fields have
no values in the records and cannot be scored.

## From the command line

```bash
# a form from a schema or spec; records from an export
python -m fillerai clean claims.schema.json export.csv -o cleaned.json
python -m fillerai clean claims.schema.json export.csv \
    --map "Loss Dt=loss_date" --map "Internal Ref=" --also invalid --skip case \
    --save --from-schema sch-...

# test a model trained on generated records against them
python -m fillerai evaluate claims.model.json cleaned.json

# or train on them instead
python -m fillerai train claims.schema.json cleaned.json -o real.model.json

# no form at all: read one off the file
python -m fillerai extract export.csv -o export.schema.json
```

## Limits and decisions

- **The browser holds the file, the server holds nothing between calls.**
  Reading, previewing and saving are three stateless requests carrying the
  same text, so an abandoned upload leaves nothing behind. The cost is that
  the file travels on every preview, within the 8 MB request limit.
- **5,000 records** per save from the UI, the same cap Train has, reported in
  the preview when it bites. The CLI has no cap.
- **1,000 records** per test from the UI: each one is a full form filled
  twice, once to score it and once to cost it.
- **Day-first dates** are read only when month-first cannot be right, unless
  the field's own format is day-first. `03/04/2024` is March 4th.
- The `case` fix capitalises each word, so `MCDONALD` becomes `Mcdonald`. It
  only touches values that were entirely in capitals.
