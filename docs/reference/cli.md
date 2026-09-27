# Command-line reference

This page lists every command in `fillerai/cli.py`, with every option, its
default, the environment variables it reads and the refusals worth knowing
about. It is a reference to look things up in; for the walkthrough of a full
pass from a form to a costed simulation, read [../process.md](../process.md).

Everything here was checked against version 0.15.0 by running
`python -m fillerai <command> --help` and the commands themselves on the files
in `examples/`.

## Contents

- [Conventions](#conventions)
- The four stages
  - [extract](#extract)
  - [inspect](#inspect)
  - [generate](#generate)
  - [check](#check)
  - [train](#train)
  - [algorithms](#algorithms)
  - [predict](#predict)
  - [evaluate](#evaluate)
  - [simulate](#simulate)
- The optional language-model commands
  - [llm status](#llm-status)
  - [propose-rules](#propose-rules)
  - [apply-rules](#apply-rules)
- The library and the bot
  - [library list](#library-list), [library show](#library-show),
    [library export](#library-export), [library delete](#library-delete),
    [library prune](#library-prune)
  - [bot list](#bot-list), [bot add](#bot-add), [bot remove](#bot-remove),
    [bot chat](#bot-chat)
- The server, the database and the people in it
  - [serve](#serve)
  - [users list](#users-list), [users add](#users-add),
    [users passwd](#users-passwd), [users role](#users-role),
    [users disable and users enable](#users-disable-and-users-enable),
    [users delete](#users-delete)
  - [tokens list](#tokens-list), [tokens add](#tokens-add),
    [tokens revoke](#tokens-revoke)
  - [db status](#db-status), [db import](#db-import)
- [Environment variables](#environment-variables)

---

## Conventions

**Invocation.** The package runs as `python -m fillerai`. The program calls
itself `fillerai` in its help and messages, so `fillerai extract` in a message
means `python -m fillerai extract`. `python -m fillerai --version` prints the
version and `--help` works at every level.

**Inputs.** Wherever a command takes a form (`source` or `schema`), a file
ending in `.html` or `.htm` is read as markup and anything else is read as
JSON: either a hand-written field spec or a schema that `extract` wrote. Record
files for `train`, `evaluate` and `simulate --records` may be `.json` (a list,
or an object with a `records` list), `.ndjson` or `.csv`. `check` is the
exception and reads JSON only.

**Outputs.** Where a command has `-o/--out`, a path writes the file (creating
its directories) and prints `wrote <path>` on stderr; `-` or no `-o` prints to
stdout. Reports meant for a person go to stderr, so stdout can be piped.
`train` writes nothing unless `-o` or `--save` is given.

**Exit codes.** Every command uses the same three.

| Code | Meaning |
|---|---|
| `0` | It did what was asked. Some commands also exit 0 when there was nothing to do (an empty listing). |
| `1` | A refusal with a one-line reason on stderr: an unknown field, user or entry, a failed check, a spending limit, the last administrator. A missing or unreadable input file also exits 1, but with a Python traceback rather than a sentence. |
| `2` | argparse rejected the command line: an unknown option, a missing argument, a value outside `choices`. |

**Where the library is.** The commands that save or read the library
(`--save` on `extract`, `generate` and `train`, and `library`, `bot`) use a
directory: `--library PATH` if given, else `$FILLERAI_HOME`, else `./.fillerai`
in the current directory. The server with accounts on keeps a per-user library
in the database instead; see [serve](#serve) and [db import](#db-import).

**Where the database is.** `users`, `tokens`, `db` and `serve` open
`--database URL` if given, else `$FILLERAI_DATABASE_URL`, else
`sqlite://<library>/fillerai.db`, where `<library>` is resolved as above. A URL
is `sqlite://<path>`, `sqlite://:memory:`, or a bare path. `postgresql://...`
is recognised and refused with an explanation, because the standard library
has no driver for it. The file and its tables are created on first use.

**Options on a command group.** For `bot`, `library`, `users`, `tokens` and
`db`, the options `--library` and `--database` belong to the group, so they go
*before* the subcommand: `fillerai library --library ./lib list`, not
`fillerai library list --library ./lib` (which exits 2).

---

## The four stages

### extract

Read a form (HTML page or JSON field spec) and write its field schema.

```
python -m fillerai extract [-o OUT] [--review-below REVIEW_BELOW] [--save] [--library PATH] source
```

| Option | Default | Meaning |
|---|---|---|
| `source` | required | An `.html`/`.htm` page, or a `.json` field spec. |
| `-o`, `--out` | stdout | Where to write the schema JSON; `-` for stdout. |
| `--review-below` | `0.7` | Confidence below which a field counts as needing review. Only used for the summary line. |
| `--save` | off | Also store the source and the schema in the library, the schema recorded as made from the source. Prints `library: <schema id>  (from <source id>)` on stderr. |
| `--library PATH` | `$FILLERAI_HOME` or `./.fillerai` | The library `--save` writes to. |

**Environment.** `FILLERAI_HOME` (with `--save`).

**Behaviour worth knowing.** When the schema goes to a file, a summary follows
on stderr: the number of fields and screens, and how many fell below
`--review-below`. When it goes to stdout, the summary is left out so the
output stays valid JSON. Always exits 0 once the source has been read.

**Example.**

```bash
python -m fillerai extract examples/claims_intake.html -o claims.schema.json
# wrote claims.schema.json
# 45 fields across 4 screen(s); 0 below confidence 0.7
```

### inspect

Show what was inferred about each field, screen by screen.

```
python -m fillerai inspect [--evidence] [--review-below REVIEW_BELOW] source
```

| Option | Default | Meaning |
|---|---|---|
| `source` | required | An `.html` page, a field spec, or a schema. |
| `--evidence` | off | Under each field, list the reasons its semantic type was chosen. |
| `--review-below` | `0.7` | Fields below this confidence are marked `?` and counted at the end. |

**Environment.** None.

**Output.** One header line (`name  (N fields, schema V)`), then per screen one
line per field: the review marker, the field name, the inferred semantic type,
the confidence, and flags (`required`, `group=...`, `N options`). Exits 0.

**Example.**

```bash
python -m fillerai inspect examples/claims_intake.html --review-below 0.8
```

### generate

Generate synthetic records that the schema would accept.

```
python -m fillerai generate [-n COUNT] [-o OUT] [-f {json,ndjson,csv}] [--seed SEED]
                            [--blank-rate BLANK_RATE] [--realistic-identifiers]
                            [--include-persona] [--check] [--save] [--library PATH]
                            [--from-schema ID] schema
```

| Option | Default | Meaning |
|---|---|---|
| `schema` | required | A schema `.json`, a field spec, or an `.html` page (extracted on the fly). |
| `-n`, `--count` | `10` | How many records to generate. |
| `-o`, `--out` | stdout | Where to write them; `-` for stdout. |
| `-f`, `--format` | `json` | `json`, `ndjson` or `csv`. |
| `--seed` | none (random) | Makes the run reproducible. |
| `--blank-rate` | `0.12` | Share of optional fields left empty; `0` fills everything. |
| `--realistic-identifiers` | off | Use full real-world ranges for SSNs, phone numbers and card numbers instead of the reserved, non-issuable ranges. |
| `--include-persona` | off | Attach the entity each record was drawn from, for debugging. |
| `--check` | off | Validate the records (constraints and coherence) before writing them. |
| `--save` | off | Also store the dataset in the library. |
| `--library PATH` | `$FILLERAI_HOME` or `./.fillerai` | The library `--save` writes to. |
| `--from-schema ID` | none | The library schema these records belong to. Without it, `--save` stores the schema again as a new entry and uses that as the parent. |

**Environment.** `FILLERAI_HOME` (with `--save`).

**Refusals.** With `--check`, any problem is printed as `FAIL ...` on stderr
(the first 25, then a count of the rest), nothing is written, and the command
exits 1. A clean check prints `checked N records: no problems`.

**Example.**

```bash
python -m fillerai generate claims.schema.json -n 500 --seed 42 --check -o claims.data.json
```

### check

Validate an existing dataset against a schema.

```
python -m fillerai check schema records
```

| Argument | Default | Meaning |
|---|---|---|
| `schema` | required | A schema `.json`, a field spec, or an `.html` page. |
| `records` | required | A **JSON** dataset: a list of records, or an object with a `records` list. CSV and NDJSON are not accepted here and fail with a JSON decoding traceback. |

**Environment.** None.

**Output and exit.** Each problem is printed on stdout as `FAIL ...`, and a
count (`N records, M problem(s)`) on stderr. Exits 1 if there is any problem,
0 otherwise, which makes it usable as a gate in a script.

**Example.**

```bash
python -m fillerai check claims.schema.json claims.data.json
# 300 records, 0 problem(s)
```

### train

Learn to autofill a form from a dataset, or compare every algorithm on it.

```
python -m fillerai train [-o OUT] [-a ALGORITHM] [--set KEY=VALUE] [--no-learned-weights]
                         [--no-rules] [--compare [ALGORITHM ...]] [--tree FIELD]
                         [--script PATH] [-v] [--holdout HOLDOUT] [--seed SEED]
                         [--ask ASK] [--seeds SEEDS] [--evaluate EVALUATE]
                         [--threshold THRESHOLD] [--save] [--library PATH]
                         [--from-dataset ID] schema records
```

| Option | Default | Meaning |
|---|---|---|
| `schema` | required | A schema `.json`, a field spec, or an `.html` page. |
| `records` | required | A `.json`, `.ndjson` or `.csv` dataset. |
| `-o`, `--out` | none | Where to write the model JSON. Without `-o` or `--save` the model is fitted, reported and discarded. |
| `-a`, `--algorithm` | `statistical` | One of `statistical`, `tree`, `forest`, `nearest`, `bayes`, `linear`. [`algorithms`](#algorithms) describes each. |
| `--set KEY=VALUE` | none | A tuning value for the chosen algorithm; repeatable. `true`/`false`, integers and floats are converted; anything else stays a string. Keys the algorithm does not read are ignored without a warning. |
| `--no-learned-weights` | off | Use the hand-picked vote weights instead of fitting them. |
| `--no-rules` | off | Skip the verified rules from the spec, to see what the algorithm manages alone. |
| `--compare [ALGORITHM ...]` | off | Fit several algorithms on the same split and print a table. With no names, every algorithm. |
| `--tree FIELD` | none | After fitting, draw the tree grown for this field (only `tree` and `forest` have one). |
| `--script PATH` | none | Write a runnable Python script that reproduces this run with the public API. |
| `-v`, `--verbose` | off | Print each stage of the fit as it happens. |
| `--holdout` | `0.25` | Share of records kept back to measure confidence on. |
| `--seed` | none | Makes the split and the fit reproducible. |
| `--ask` | `3` | How many fields to suggest the agent types first. |
| `--seeds` | none | Comma-separated field names to use as those fields instead. |
| `--evaluate PATH` | none | A second dataset to score the finished model on; prints one headline line. |
| `--threshold` | `0.7` | Confidence at which a prediction is offered (used by `--evaluate` and `--compare`). |
| `--save` | off | Also store the model in the library. |
| `--library PATH` | `$FILLERAI_HOME` or `./.fillerai` | The library `--save` writes to. |
| `--from-dataset ID` | none | The library dataset the records came from. Without it, `--save` also stores the schema and the dataset, so the model's lineage is complete. |

**Tuning keys** (`--set`), with their defaults, as `fillerai algorithms` prints
them:

| Algorithm | Keys |
|---|---|
| `statistical` (default) | `min_lambda=0.1`, `max_predictors=6` |
| `tree` | `max_depth=4` |
| `forest` | `trees=10`, `max_depth=5` |
| `nearest` | `neighbours=12`, `rows=600` |
| `bayes` | `evidence=5` |
| `linear` | `epochs=12`, `learning_rate=0.1`, `l2=2e-05`, `buckets=4096`, `prune=0.05`, `max_weights=40000` |

**Environment.** `FILLERAI_HOME` (with `--save`).

**Output.** The field-by-field report goes to stderr: for each field, what the
model will lean on (`a rule`, `other fields`, `the usual value`, or `you`), a
strength, and a detail. Then the suggested seed fields, the engine's summary,
and the learned vote weights. With `--compare`, the table goes to stdout
instead.

**Refusals and quirks.**

- An empty dataset exits 1 (`that dataset has no records in it`).
- `--compare` with an unknown name exits 1 and lists the valid ones. The names
  after `--compare` are not checked by argparse.
- `--compare` returns after the table: `-o`, `--save`, `--script`, `--tree`
  and `--evaluate` are ignored in that mode.
- `--tree` with an algorithm that grows no tree prints a note and carries on.
- A `--set` value without `=` exits 1.

**Example.**

```bash
python -m fillerai train claims.schema.json claims.data.json --seed 42 -o claims.model.json
python -m fillerai train claims.schema.json claims.data.json --seed 42 --compare statistical tree forest
```

### algorithms

List the algorithms `train --algorithm` accepts, and what each does.

```
python -m fillerai algorithms [--recipe]
```

| Option | Default | Meaning |
|---|---|---|
| `--recipe` | off | Also print each algorithm's steps, numbered. |

**Environment.** None. Exits 0.

**Output.** One block per algorithm: its name (the default marked `*`), its
label, a paragraph on what it does, and its `--set` keys with defaults.

**Example.**

```bash
python -m fillerai algorithms --recipe
```

### predict

Fill in the rest of a form from the few fields already typed.

```
python -m fillerai predict [--set FIELD=VALUE] [-f {text,json,record}] [-o OUT]
                           [--threshold THRESHOLD] model
```

| Option | Default | Meaning |
|---|---|---|
| `model` | required | A model file written by `train -o` (or `library export` of a model). |
| `--set FIELD=VALUE` | none | A field the agent has already typed; repeatable. |
| `-f`, `--format` | `text` | `text`: a table of every field. `json`: the given values and every prediction with its confidence, basis and reasons. `record`: only the filled form, as JSON. |
| `-o`, `--out` | stdout | Output path for `json` and `record`; `-` for stdout. Ignored for `text`. |
| `--threshold` | `0.7` | Confidence at which a prediction is filled in. In `text`, predictions below it are shown with a `?`; in `record`, they are left out. |

**Environment.** None.

**Refusals.** A `--set` without `=`, or a field name the model does not know,
exits 1. Values are not checked against the field's options.

**Example.**

```bash
python -m fillerai predict claims.model.json --set home_postal_code=11238 -f record
```

### evaluate

Score a model on records it has not seen.

```
python -m fillerai evaluate [--ask ASK] [--seeds SEEDS] [--threshold THRESHOLD] [-o OUT] model records
```

| Option | Default | Meaning |
|---|---|---|
| `model` | required | A model written by `train`. |
| `records` | required | A `.json`, `.ndjson` or `.csv` dataset. |
| `--ask` | `3` | How many seed fields the agent is assumed to type (chosen by the model). |
| `--seeds` | none | Comma-separated field names to use as the seeds instead. |
| `--threshold` | `0.7` | Confidence at which a prediction is offered. |
| `-o`, `--out` | none | Also write the full report as JSON. |

**Environment.** None. Exits 0.

**Output.** The seeds; a headline (how much of the rest was filled and how
accurately); a reliability table showing, for each confidence band, how often
predictions in that band were right; and the eight weakest fields.

**Example.**

```bash
python -m fillerai generate claims.schema.json -n 100 --seed 9 -o fresh.json
python -m fillerai evaluate claims.model.json fresh.json -o eval.json
```

### simulate

Play a model against forms it has never seen and cost the result in time and
keystrokes.

```
python -m fillerai simulate [-n FORMS] [--records RECORDS] [--ask ASK] [--seeds SEEDS]
                            [--threshold THRESHOLD] [--seed SEED] [--show] [-o OUT] model
```

| Option | Default | Meaning |
|---|---|---|
| `model` | required | A model written by `train`. |
| `-n`, `--forms` | `25` | How many fresh forms to generate from the model's own schema. Ignored with `--records`. |
| `--records` | none | Use this dataset (`.json`, `.ndjson`, `.csv`) as the forms instead of generating them. |
| `--ask` | `3` | How many fields the agent is assumed to type. |
| `--seeds` | none | Comma-separated field names to type instead. |
| `--threshold` | `0.7` | Confidence at which a prediction is filled in. |
| `--seed` | none | Makes the generated forms reproducible. |
| `--show` | off | Print the first form field by field: typed, filled, held back or left to type, and whether each filled value was right. |
| `-o`, `--out` | none | Also write the full report, with the model's provenance, as JSON. |

**Environment.** None.

**Refusals.** An empty `--records` file exits 1 (`there are no forms to
simulate`), as does a `--seeds` name the model does not know.

**Output.** Which model played and what it learned from, a headline, the
keystrokes and time saved in total, and the effort constants the costing
assumes (`counted as: ...`).

**Example.**

```bash
python -m fillerai simulate claims.model.json -n 50 --ask 3 --seed 7 --show
```

---

## The optional language-model commands

These are the only commands that can talk to a network, and only when a key is
set and a run is not a dry run or a replay. Each imports the language-model
code inside the command, so the rest of the CLI never loads it. The provider
is chosen in this order: `--provider`; `$FILLERAI_LLM_PROVIDER`; a model name
that belongs to one provider (`--model` or `$FILLERAI_LLM_MODEL`); exactly one
of `$ANTHROPIC_API_KEY` or `$OPENAI_API_KEY` being set; the prefix of
`$FILLERAI_LLM_KEY` (`sk-ant-` or `sk-`); otherwise Anthropic.
[../process.md §7](../process.md#7-asking-a-model-for-the-rules-optional-off-by-default)
explains the workflow.

### llm status

Show which service and models the language-model features would use, and
whether a key is set, without sending anything.

```
python -m fillerai llm status [--provider {anthropic,openai}]
```

| Option | Default | Meaning |
|---|---|---|
| `--provider` | chosen from the environment | Report on this service instead. |

**Environment.** `FILLERAI_LLM_KEY`, `FILLERAI_LLM_PROVIDER`,
`FILLERAI_LLM_MODEL`, `FILLERAI_LLM_BASE_URL`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`.

**Output.** The provider and the reason it was chosen; the model for each task
(`rules`, `typing`, `chat`) with the key shown only as `set (…last4)` or `not
set`; the transport (the provider's SDK if installed, else `urllib`); the
endpoint if `FILLERAI_LLM_BASE_URL` points elsewhere; and, with no key, which
variable to export. Always exits 0.

**Example.**

```bash
python -m fillerai llm status --provider openai
#   provider   OpenAI (asked for on the command line)
#   rules    gpt-5                key not set
#   ...
```

### propose-rules

Ask a language model which business rules a form implies, then check each
proposal against generated records before showing it.

```
python -m fillerai propose-rules [-o OUT] [--dry-run] [--model MODEL] [--provider {anthropic,openai}]
                                 [--max-spend USD] [--sample SAMPLE] [--batch FIELDS]
                                 [--replay PATH] [--review-below REVIEW_BELOW] source
```

| Option | Default | Meaning |
|---|---|---|
| `source` | required | An `.html` page or a `.json` field spec (a schema also loads). |
| `-o`, `--out` | none | Where to write the proposals as JSON, for `apply-rules`; `-` for stdout. |
| `--dry-run` | off | Print the estimated tokens and cost, and send nothing. Needs no key. |
| `--model` | per provider (`claude-opus-5` or `gpt-5` for rules) | Override the model for this run. |
| `--provider` | chosen from the environment | `anthropic` or `openai`. |
| `--max-spend USD` | `1.0` | Refuse the run if the estimate is above this. |
| `--sample` | `200` | How many records to generate to test each proposed rule against. |
| `--batch FIELDS` | `60` | On a form with more candidate fields than this, ask in groups of this size. `0` asks about the whole form in one call. |
| `--replay PATH` | none | Answer from a recorded exchange (a JSON file with a `recordings` list) instead of calling out. Needs no key. |
| `--review-below` | `0.7` | Kept rules below this confidence are marked for review in the report. |

**Environment.** As for [`llm status`](#llm-status).

**Output.** The estimate on stderr, then on stdout the kept rules (field, rule,
confidence, the model's reason) and the dropped ones with why they failed the
check. With `-o PATH`, a hint for the `apply-rules` command follows.

**Refusals (exit 1, `could not propose rules: ...`).** No key for the chosen
provider; an estimate above `--max-spend`; a transport error; a reply that
cannot be read.

**Example.**

```bash
python -m fillerai propose-rules examples/member_enrollment.fields.json --dry-run
python -m fillerai propose-rules examples/member_enrollment.fields.json \
    --replay tests/fixtures/llm/rules_member_enrollment.json -o rules.json
```

### apply-rules

Merge reviewed rule proposals into a field spec. Nothing is sent anywhere.

```
python -m fillerai apply-rules [-o OUT] [--above CONFIDENCE] spec rules
```

| Option | Default | Meaning |
|---|---|---|
| `spec` | required | The `.json` field spec to merge into. HTML is not accepted here. |
| `rules` | required | The file `propose-rules -o` wrote. Only its `kept` list is read. |
| `-o`, `--out` | stdout | Where to write the merged spec; `-` for stdout. |
| `--above CONFIDENCE` | `0.0` | Only merge proposals at least this confident. |

**Environment.** None.

**Output.** Each proposal that could not be applied is listed as `skipped ...`
on stderr, then `N rule(s) merged into M field(s)`. Exits 0.

**Example.**

```bash
python -m fillerai apply-rules examples/member_enrollment.fields.json rules.json --above 0.85 -o merged.json
```

---

## The library and the bot

### library

Browse and tidy the directory library that `--save` writes to. The group
option `--library PATH` (default `$FILLERAI_HOME` or `./.fillerai`) goes before
the subcommand. Entry ids look like `mdl-20260927-143316760-9557`; the prefix
names the kind (`src`, `sch`, `dat`, `mdl`, and so on). Any `StoreError`, such
as an unknown id, exits 1. This command reads the directory library only, not
the per-user libraries a server with accounts keeps in the database.

**Environment.** `FILLERAI_HOME`, for every subcommand.

#### library list

List entries, newest first.

```
python -m fillerai library [--library PATH] list [-k KIND] [--under ID] [-n LIMIT]
```

| Option | Default | Meaning |
|---|---|---|
| `-k`, `--kind` | all | Only `source`, `schema`, `dataset`, `model`, `script` or `template`. |
| `--under ID` | none | Only entries made from this one. |
| `-n`, `--limit` | all | At most this many. |

Prints the library path, one line per entry (id, kind, time, name) with its
metadata beneath, and totals by kind with the size. When nothing matches, it
says `the library at ... is empty` and exits 0, even if only the filter
matched nothing.

```bash
python -m fillerai library list -k model -n 5
```

#### library show

Show one entry, what it was made from, and what was made from it.

```
python -m fillerai library [--library PATH] show id
```

Prints the entry's kind, name, time, size and metadata, then `made from` (the
chain upward to the source) and `used to make` (its direct children).

```bash
python -m fillerai library show dat-20260927-143247543-8a9c
```

#### library export

Write one entry's contents out as JSON.

```
python -m fillerai library [--library PATH] export [-o OUT] id
```

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--out` | stdout | Output path; `-` for stdout. |

An exported model can be passed straight to `predict`, `evaluate` or
`simulate`.

```bash
python -m fillerai library export mdl-20260927-143316760-9557 -o claims.model.json
```

#### library delete

Remove one entry.

```
python -m fillerai library [--library PATH] delete [--cascade] id
```

| Option | Default | Meaning |
|---|---|---|
| `--cascade` | off | Also remove everything made from it. |

Without `--cascade`, an entry that anything other than a script was made from
is refused (exit 1) rather than leaving its descendants without provenance. A
model's script is always removed with the model.

```bash
python -m fillerai library delete sch-20260927-143247046-b219 --cascade
```

#### library prune

Drop the oldest entries of each kind.

```
python -m fillerai library [--library PATH] prune [--keep KEEP]
```

| Option | Default | Meaning |
|---|---|---|
| `--keep` | `50` | How many of each kind to keep. |

An old entry that something was made from is kept regardless, so lineage is
never broken. Prints how many were removed and exits 0.

```bash
python -m fillerai library prune --keep 10
```

### bot

Manage Bot Builder chat templates in the directory library, and talk to the
bot at the terminal. The group option `--library PATH` (default
`$FILLERAI_HOME` or `./.fillerai`) goes before the subcommand. A malformed
template or a store error exits 1. The template format and the chat contract
are in [../bot-builder.md](../bot-builder.md).

**Environment.** `FILLERAI_HOME`, for every subcommand. The terminal chat does
not use a language model.

#### bot list

List the templates in the library, and the bundled starters.

```
python -m fillerai bot [--library PATH] list
```

One line per template (key, name, number of fields, required fields), then
the starter names (`address_change`, `document_request`). With no templates,
it suggests `bot add --starter` and exits 0.

```bash
python -m fillerai bot list
```

#### bot add

Add a template, or replace the one with the same key.

```
python -m fillerai bot [--library PATH] add [--starter NAME] [file]
```

| Option | Default | Meaning |
|---|---|---|
| `file` | none | A template `.json`. |
| `--starter NAME` | none | A bundled starter: `address_change` or `document_request`. Takes precedence over `file`. |

Prints `saved <key> as <entry id>`. Giving neither, or an unknown starter,
exits 1.

```bash
python -m fillerai bot add --starter address_change
```

#### bot remove

Remove a template by its key.

```
python -m fillerai bot [--library PATH] remove key
```

Prints the removed entry ids; an unknown key exits 1.

```bash
python -m fillerai bot remove address_change
```

#### bot chat

Hold a conversation with the bot at the terminal.

```
python -m fillerai bot [--library PATH] chat [--current [FIELD=VALUE ...]]
```

| Option | Default | Meaning |
|---|---|---|
| `--current FIELD=VALUE ...` | none | What the host application already holds, so the bot can show each change as before and after. Pairs without `=` are ignored. |

Uses the library's templates, or the starters if there are none. Type at the
`you>` prompt; a number clicks the action with that number; an empty line or
end of input stops. When the bot submits, the values are folded into
`--current` and the conversation carries on. Exits 0.

```bash
python -m fillerai bot chat --current street_address="1 Old Rd" city=Boston state=MA
```

---

## The server, the database and the people in it

### serve

Start the web UI, the `/v1` integration API and, by default, the sample
application next to it. It runs until Ctrl-C.

```
python -m fillerai serve [-p PORT] [--host HOST] [--open] [-v] [--library PATH]
                         [--database URL] [--cors-origin ORIGIN] [--no-auth]
                         [--bot-llm] [--bot-transcribe] [--no-sample-app]
                         [--sample-port SAMPLE_PORT] [--sample-user USERNAME]
```

| Option | Default | Meaning |
|---|---|---|
| `-p`, `--port` | `8000` | Port for the UI and API. |
| `--host` | `127.0.0.1` | Bind address. The default keeps the UI on this machine. |
| `--open` | off | Open a browser window. |
| `-v`, `--verbose` | off | Log each request. |
| `--library PATH` | `$FILLERAI_HOME` or `./.fillerai` | The directory library. With accounts on, it also locates the default database. |
| `--database URL` | `$FILLERAI_DATABASE_URL` or `sqlite://<library>/fillerai.db` | The database for accounts, tokens and per-user libraries. |
| `--cors-origin ORIGIN` | `*` with accounts on; none with `--no-auth` | Let a browser app on this origin call `/v1`. Repeatable; `*` for any. Giving it replaces the default. |
| `--no-auth` | off (accounts on) | No login and no accounts: one library for one person. Allowed only on a loopback `--host`. |
| `--bot-llm` | off | Let the bot read chat phrases with the configured language model. Sends what end users type to the provider. |
| `--bot-transcribe` | off | Let the chat record audio in the browser and transcribe it here with an OpenAI key. Sends voices to OpenAI. |
| `--no-sample-app` | off | Do not start the sample application (Northwind Mutual). |
| `--sample-port` | `8100` | The sample application's port. |
| `--sample-user USERNAME` | first active administrator | Whose bot templates the sample application shows. |

**Environment.** `FILLERAI_HOME`, `FILLERAI_DATABASE_URL`,
`FILLERAI_ADMIN_PASSWORD`, `FILLERAI_BOT_LLM`, `FILLERAI_BOT_TRANSCRIBE`, and
all the language-model and transcription variables in the
[table below](#environment-variables). Keys typed into the UI's Settings are
laid over the environment for that user and are not written to disk.

**What happens at start.**

- With accounts on (the default), the database is opened. If it has no users,
  an administrator called `admin` is created and its password is printed once
  (or taken from `$FILLERAI_ADMIN_PASSWORD`); it must be changed at first
  sign-in. Then anything in the directory library that is not already in the
  first active administrator's database library is copied there. This happens
  on every start, so entries saved from the CLI reach that administrator the
  next time the server starts.
- With `--no-auth` and no `--database`, the server uses the directory library
  and no database.
- The sample application is issued a fresh API token for `--sample-user` (the
  previous one is removed) and keeps its demo data under
  `<library>/sample-app/`. If there is no such user, or no administrator, or
  its port is taken, the server still starts and says why the sample
  application did not.
- The banner lists the URL, the database or library, the accounts state, the
  `/v1` address and allowed browser origins, the JavaScript client and chat
  demo pages, and the sample application.

**Refusals.** `--no-auth` with a non-loopback `--host` (such as `0.0.0.0`)
exits 1 before binding. A `--port` already in use ends in an `OSError`
traceback and exit 1; a taken `--sample-port` only skips the sample
application.

**Example.**

```bash
python -m fillerai serve --no-auth --no-sample-app -p 8000
python -m fillerai serve --host 0.0.0.0 --cors-origin https://claims.example.com
```

See [../process.md §9](../process.md#9-running-it-for-other-people) and
[../integration.md](../integration.md).

### users

Manage the accounts that can sign in to the UI, directly in the database. This
works from a shell with nothing but the database file, which is the way back in
when nobody can sign in. The group options `--database URL` and
`--library PATH` go before the subcommand and locate the database as described
under [Conventions](#conventions).

**Environment.** `FILLERAI_DATABASE_URL`, `FILLERAI_HOME`, for every
subcommand.

**Rules every subcommand enforces (exit 1 with the reason).** A username is 2
to 32 characters of lowercase letters, digits, `.`, `-` or `_`, starting with a
letter or digit; names are lowercased before use. A password is at least 8
characters and not the username. The last active administrator cannot be
demoted, disabled or deleted. An unknown username is refused.

#### users list

List every account.

```
python -m fillerai users [--database URL] [--library PATH] list
```

One line per account: username, role, last sign-in (or `never signed in`), and
`(disabled)` or `(must change password)` where they apply; totals on stderr.
With no accounts it says how to make the first and exits 0.

```bash
python -m fillerai users list
```

#### users add

Create an account.

```
python -m fillerai users [--database URL] [--library PATH] add [--admin] [--name NAME]
                         [--password PASSWORD] [--must-change] username
```

| Option | Default | Meaning |
|---|---|---|
| `username` | required | The sign-in name. |
| `--admin` | off (role `user`) | Make an administrator, who can manage other users. |
| `--name` | none | Display name in the UI. |
| `--password` | generated | The password. Without it one is generated, printed once, and must be changed at first sign-in. |
| `--must-change` | off | Require a new password at first sign-in even when `--password` was given. |

A duplicate username exits 1.

```bash
python -m fillerai users add alice --admin --name "Alice Ng"
#   created alice (admin)
#   password: a65z-ruhy-kkt9-aqvz
```

#### users passwd

Set somebody's password and end all their sessions.

```
python -m fillerai users [--database URL] [--library PATH] passwd [--password PASSWORD] username
```

| Option | Default | Meaning |
|---|---|---|
| `--password` | generated | The new password. A generated one is printed once and must be changed at next sign-in; a given one need not be. |

```bash
python -m fillerai users passwd bob
```

#### users role

Make somebody an administrator, or not.

```
python -m fillerai users [--database URL] [--library PATH] role username {admin,user}
```

Demoting the last active administrator exits 1.

```bash
python -m fillerai users role bob admin
```

#### users disable and users enable

Turn an account off without deleting it, or back on.

```
python -m fillerai users [--database URL] [--library PATH] disable username
python -m fillerai users [--database URL] [--library PATH] enable username
```

Disabling the last active administrator exits 1. Enabling an account that is
already enabled succeeds silently.

```bash
python -m fillerai users disable bob
```

#### users delete

Remove an account together with its library, sessions and API tokens.

```
python -m fillerai users [--database URL] [--library PATH] delete [--yes] username
```

| Option | Default | Meaning |
|---|---|---|
| `--yes` | off | Confirm that their library entries go too. |

If the account owns library entries and `--yes` is missing, it says how many
and exits 1 without deleting. Deleting the last active administrator exits 1.

```bash
python -m fillerai users delete bob --yes
```

### tokens

Issue and revoke API tokens, the credential an outside application uses to call
`/v1` as an account. The group options `--database URL` and `--library PATH`
go before the subcommand. Tokens look like `flr_<id>_<secret>`; only a hash is
stored. See [../integration.md](../integration.md).

**Environment.** `FILLERAI_DATABASE_URL`, `FILLERAI_HOME`, for every
subcommand.

#### tokens list

List live tokens.

```
python -m fillerai tokens [--database URL] [--library PATH] list [--all] [username]
```

| Option | Default | Meaning |
|---|---|---|
| `username` | all accounts | Only this account's tokens. An unknown user exits 1. |
| `--all` | off | Include revoked and expired tokens, marked as such. |

One line per token: name, `flr_<id>...` prefix, owner, last use, and the model
it is limited to. With none, it says how to issue one and exits 0.

```bash
python -m fillerai tokens list alice --all
```

#### tokens add

Issue a token for an account. The secret is printed once.

```
python -m fillerai tokens [--database URL] [--library PATH] add [--name NAME] [--days DAYS]
                          [--model ID] username
```

| Option | Default | Meaning |
|---|---|---|
| `username` | required | The account whose library the token reads. |
| `--name` | `integration` | A label for the listing, up to 60 characters. |
| `--days` | never expires | Expire it after this many days. Zero or less exits 1. |
| `--model ID` | any model | Limit it to one model in that library. The id is not checked when the token is issued. |

An account may hold at most 25 live tokens; the 26th exits 1. A lost token is
reissued, not recovered.

```bash
python -m fillerai tokens add alice --name "claims portal" --days 30
#   issued 'claims portal' for alice
#   expires 2026-10-27T14:34:13+00:00
#
#   flr_affaa5215b3a_QXOBy1mCm6R978_...
```

#### tokens revoke

Turn a token off for good.

```
python -m fillerai tokens [--database URL] [--library PATH] revoke token
```

`token` may be the token's id, its `flr_<id>` prefix as the listing shows it,
or its name. Only live tokens are matched. No match exits 1; a name shared by
several tokens exits 1 and asks for the prefix instead.

```bash
python -m fillerai tokens revoke "claims portal"
```

### db

Report on the database and move a directory library into it. The group
options `--database URL` and `--library PATH` go before the subcommand. A
database that cannot be opened (including a `postgresql://` URL) exits 1.

**Environment.** `FILLERAI_DATABASE_URL`, `FILLERAI_HOME`, for every
subcommand.

#### db status

Say where the database is and what is in it.

```
python -m fillerai db [--database URL] [--library PATH] status
```

Prints the backend and URL, the schema version and table count, the number of
users and administrators, and the number of library entries and owners.
Exits 0.

```bash
python -m fillerai db status
#   SQLiteDatabase at sqlite:///srv/fillerai/.fillerai/fillerai.db
#   schema version 2, 7 table(s)
#   2 user(s), 1 administrator(s)
#   8 library entr(ies) across 2 owner(s)
```

#### db import

Copy a directory library into a library in the database, keeping ids and
lineage.

```
python -m fillerai db [--database URL] [--library PATH] import [--from PATH] [--user USERNAME]
```

| Option | Default | Meaning |
|---|---|---|
| `--from PATH` | the group's `--library`, else `$FILLERAI_HOME`, else `./.fillerai` | The directory library to read. |
| `--user USERNAME` | the unowned library | Whose library to copy into. Without it, entries go to the unowned library that a `--no-auth` server with `--database` uses. An unknown user exits 1. |

Prints how many entries were copied, were already there, or could not be read.
Running it again copies nothing new.

```bash
python -m fillerai db import --from ./.fillerai --user alice
```

---

## Environment variables

Every variable the package reads, found by searching it for `os.environ`.
Nothing is read from any other configuration file.

| Variable | Read by | Meaning | Default |
|---|---|---|---|
| `FILLERAI_HOME` | `store.py`; every command with `--library` or `--save`, and the default database location | The directory library. `--library` overrides it. | `./.fillerai` |
| `FILLERAI_DATABASE_URL` | `db.py`; `serve`, `users`, `tokens`, `db` | The database URL. `--database` overrides it. | `sqlite://<library>/fillerai.db` |
| `FILLERAI_ADMIN_PASSWORD` | `auth.py`; `serve` on an empty database | The first administrator's password, instead of a generated one. | generated and printed once |
| `FILLERAI_BOT_LLM` | `web/server.py`; `serve` | Same as `--bot-llm`. On unless empty, `0`, `false` or `no`. | off |
| `FILLERAI_BOT_TRANSCRIBE` | `web/server.py`; `serve` | Same as `--bot-transcribe`, read the same way. | off |
| `FILLERAI_LLM_KEY` | `llm/config.py`; `llm status`, `propose-rules`, `serve` | The API key for whichever provider is chosen. Takes precedence over the provider's own variable. Its prefix is a last-resort hint for the provider. For transcription it is used only if it looks like an OpenAI key. | none |
| `FILLERAI_LLM_PROVIDER` | `llm/config.py`; same | `anthropic` or `openai`. `--provider` overrides it. | worked out, else `anthropic` |
| `FILLERAI_LLM_MODEL` | `llm/config.py`; same | One model for every task, overriding the per-task defaults. `--model` overrides it. | per task and provider (below) |
| `FILLERAI_LLM_BASE_URL` | `llm/config.py`; same | Point every language-model call at another deployment or gateway. The provider's path is appended. | `https://api.anthropic.com` or `https://api.openai.com` |
| `ANTHROPIC_API_KEY` | `llm/providers.py`; same | Anthropic's key, used when `FILLERAI_LLM_KEY` is not set. | none |
| `OPENAI_API_KEY` | `llm/providers.py`, `llm/transcribe.py`; same, and `serve --bot-transcribe` | OpenAI's key, used when `FILLERAI_LLM_KEY` is not set, and the preferred key for transcription. | none |
| `FILLERAI_TRANSCRIBE_MODEL` | `llm/transcribe.py`; `serve --bot-transcribe` | The transcription model. | `gpt-4o-mini-transcribe` |
| `FILLERAI_TRANSCRIBE_BASE_URL` | `llm/transcribe.py`; `serve --bot-transcribe` | Where audio is sent. Separate from `FILLERAI_LLM_BASE_URL` so audio never goes to a gateway meant for the other provider. | `https://api.openai.com` |
| `FILLERAI_URL` | `sampleapp/app.py`; only `python -m fillerai.sampleapp` run on its own | The FillerAI server the sample application calls. | `http://localhost:8000` |
| `FILLERAI_TOKEN` | `sampleapp/app.py`; same | The API token the sample application uses. Not needed against a `--no-auth` server. | none |
| `SAMPLE_APP_QUIET` | `sampleapp/app.py`; same | `1` silences its request log. | not set |

The per-task default models are:

| Task | Anthropic | OpenAI |
|---|---|---|
| `rules` (`propose-rules`) | `claude-opus-5` | `gpt-5` |
| `typing` | `claude-sonnet-5` | `gpt-5-mini` |
| `chat` (`serve --bot-llm`) | `claude-haiku-4-5` | `gpt-5-mini` |

Base URLs are passed to the provider SDKs explicitly, so the SDKs' own
variables, such as `ANTHROPIC_BASE_URL`, have no effect on FillerAI. Use
`FILLERAI_LLM_BASE_URL` instead.
