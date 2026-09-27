# Data formats

Every format FillerAI writes to disk, keeps in its database, or hands to
another program: what the keys are, what types they hold, and which rules a
reader can rely on. Why each format looks the way it does is in
[architecture.md](../architecture.md) and, for the bot, in
[bot-builder.md](../bot-builder.md). This page is the list of fields.

Everything here was read off the code at version 0.15.0. Each example is
real output: produced by running the CLI on the files in `examples/`, then
trimmed to a few fields where marked.

## Contents

1. [The field schema](#1-the-field-schema) — `fillerai/schema.py`
2. [The field spec](#2-the-field-spec) — `fillerai/extract/spec.py`
3. [Datasets](#3-datasets) — `fillerai/generate/dataset.py`, `render.py`
4. [The trained model](#4-the-trained-model) — `fillerai/train/model.py`, `train/algos/`
5. [The library](#5-the-library) — `fillerai/store.py`, `fillerai/dbstore.py`
6. [The database](#6-the-database) — `fillerai/db.py`
7. [Passwords, sessions and API tokens](#7-passwords-sessions-and-api-tokens) — `fillerai/auth.py`, `fillerai/tokens.py`
8. [Bot templates and conversation state](#8-bot-templates-and-conversation-state) — `fillerai/bot/`
9. [Version numbers at a glance](#9-version-numbers-at-a-glance)

Conventions used throughout: every JSON file is UTF-8, written with
`ensure_ascii=False`. Every time is an ISO 8601 string in local time with its
offset, to the second (`2026-09-27T14:34:05+00:00`). Every `from_dict` in the
code ignores keys it does not know, so an extra key never breaks a reader.

---

## 1. The field schema

`FormSchema.to_dict()` / `to_json()`. The one format every stage agrees on;
see [architecture.md §2](../architecture.md#2-the-contract-filleraischemapy)
for why it is the contract. `fillerai extract` writes it, and every command
that takes a `schema` argument reads it.

### 1.1 Top level

| Key | Type | Always present | Meaning |
|---|---|---|---|
| `schema_version` | string | yes | `SCHEMA_VERSION`, currently `"1.0"`. |
| `name` | string | yes | The form's name. Defaults to `"form"` when read without one. |
| `source` | object | yes | Where the schema came from. `{"kind": "html" \| "spec"}`, plus `"path"` when it was read from a file. Free-form; nothing downstream depends on it. |
| `screens` | array of Screen | yes (may be empty) | The form's pages or steps, in order. |
| `fields` | array of Field | yes | The fields, in the form's own order. |

A **Screen** is `{"id": string, "title": string | null}`.

### 1.2 Field

Keys are written in this order. Optional keys are left out when empty, so a
missing key and its default mean the same thing.

| Key | Type | Written | Meaning |
|---|---|---|---|
| `name` | string | always | **The stable join key**: the same string in the dataset columns, the model, the simulator and a bot template. The control's `name` where there is one, otherwise a slug. |
| `label` | string or null | always | The label a person reads. |
| `semantic_type` | string | always | What the field means. One of the list in §1.3. Defaults to `"unknown"`. |
| `data_type` | string | always | One of `string`, `integer`, `number`, `boolean`, `date`, `datetime`, `time` (`DATA_TYPES`). Defaults to `"string"`. |
| `control` | string | always | The control: `text`, `textarea`, `select`, `radio`, `checkbox`, `date`, `number`, … Defaults to `"text"`. |
| `screen` | string | when set | The `id` of the screen the field is on. |
| `group` | string | when set | Coherence group. Fields in one group describe the same entity (one address); different groups are generated independently (billing vs shipping). |
| `options` | array of Option | when non-empty | A closed list. An **Option** is `{"value": string, "label": string}`; `label` is written as the value when there was none. The value is what a record holds. |
| `constraints` | object | when any is set | See §1.4. |
| `placeholder` | string | when set | |
| `help_text` | string | when set | |
| `confidence` | number | always | How sure inference is of `semantic_type`, 0.0 to 1.0, rounded to three places. `1.0` means a field spec declared it. |
| `evidence` | array of string | when non-empty | Why inference chose the type, one line per signal. |
| `extra` | object | when non-empty | Source-specific detail (an `autocomplete` token, ARIA attributes). Never used for generation. |
| `derived` | object | when set | A rule the form declares. See §1.5. |

### 1.3 Semantic types

`SEMANTIC_TYPES` in `fillerai/schema.py`, 47 of them. Generation is driven by
this value alone, so a new one needs a member here and a renderer in
`fillerai/generate/render.py`.

| Group | Types |
|---|---|
| identity | `first_name`, `middle_name`, `last_name`, `full_name`, `prefix`, `suffix`, `date_of_birth`, `age`, `gender`, `ssn` |
| contact | `email`, `phone`, `phone_mobile`, `url` |
| address | `street_address`, `address_line2`, `city`, `state`, `postal_code`, `country` |
| organisation | `company`, `job_title`, `department`, `employee_id` |
| financial | `credit_card_number`, `credit_card_expiry`, `credit_card_cvv`, `account_number`, `routing_number`, `currency_amount`, `iban` |
| insurance / claims | `policy_number`, `claim_number`, `group_number`, `member_id`, `diagnosis_code` |
| generic | `date`, `datetime`, `time`, `integer`, `decimal`, `percentage`, `boolean`, `enum`, `free_text`, `password`, `unknown` |

The schema loader does not check `semantic_type` against this list; a value
it does not know is carried through and rendered as free text. A bot
template does check it (§8.2).

### 1.4 Constraints

| Key | Type | Default | Meaning |
|---|---|---|---|
| `required` | boolean | `false` | The form will not submit without it. |
| `min_length` | integer | none | |
| `max_length` | integer | none | |
| `pattern` | string | none | A regular expression, as the form states it. |
| `minimum` | number | none | |
| `maximum` | number | none | |
| `step` | number | none | |
| `multiple` | boolean | `false` | The control accepts more than one value. |
| `read_only` | boolean | `false` | Nothing should write to it. The generator leaves it out of records. |

A constraint is written only when the source stated it: `None` means
unconstrained, never zero. `false` booleans are left out. The test is by
identity, so `"minimum": 0` is kept.

### 1.5 Declared rules: `derived`

A field that follows others carries the rule as `derived`:

| Key | Type | Written | Meaning |
|---|---|---|---|
| `sources` | array of string | always | The field names this one follows, in order. |
| `table` | object | always | Maps a **key** (below) to the array of values this field may then hold. One value decides the field; several narrow it. |
| `otherwise` | array of string | when non-empty | What the field may hold for a combination the table does not list. With none, the field is generated normally. |

A table key is the sources' values, each stripped and lower-cased, joined
with `|` in the order `sources` gives (`Derived.key`). So a rule on
`["coverage_tier", "vehicle_use"]` has keys like `gold|commute`. In a field
spec the same rule is written with `follows` / `when` (§2.2), which is how
people write it; `derived` is how the schema stores it.

### 1.6 Example

From `fillerai extract examples/auto_insurance_quote.fields.json` (two of 54
fields shown):

```json
{
  "schema_version": "1.0",
  "name": "auto_insurance_quote",
  "source": {"kind": "spec", "path": "examples/auto_insurance_quote.fields.json"},
  "screens": [
    {"id": "driver", "title": "Driver"},
    {"id": "vehicle", "title": "Vehicle"}
  ],
  "fields": [
    {
      "name": "garaging_postal_code",
      "label": "Garaging ZIP",
      "semantic_type": "postal_code",
      "data_type": "string",
      "control": "text",
      "screen": "driver",
      "group": "driver",
      "constraints": {"required": true, "pattern": "\\d{5}"},
      "confidence": 0.8,
      "evidence": ["name matches /(zip|postal|postcode|post_?code)/"]
    },
    {
      "name": "sr22_required",
      "label": "SR-22 Filing Required",
      "semantic_type": "boolean",
      "data_type": "boolean",
      "control": "select",
      "screen": "driver",
      "options": [
        {"value": "Yes", "label": "Yes"},
        {"value": "No", "label": "No"}
      ],
      "confidence": 0.72,
      "evidence": ["options are yes/no"],
      "derived": {
        "sources": ["license_status"],
        "table": {"valid": ["No"], "restricted": ["Yes"], "suspended": ["Yes"]}
      }
    }
  ]
}
```

### 1.7 Version and compatibility

- `SCHEMA_VERSION` is `"1.0"`. It is bumped on a breaking change.
- `FormSchema.from_dict` compares only the **major** version. A schema whose
  major differs is refused with a `ValueError` naming both versions; it is
  never half-loaded. A schema with no `schema_version` is read as the current
  version.
- Readers ignore unknown keys, so a new optional key is a minor change and
  needs no bump.
- A read schema keeps the `schema_version` it was written with.

---

## 2. The field spec

A compact JSON document a person writes by hand when the markup cannot be
shared. `fillerai/extract/spec.py` turns it into the same `FormSchema` the
HTML path produces, then inference fills in whatever it leaves out.

**How a file is told apart from a schema:** `load_file` checks for a
`schema_version` key. With it, the file is read as a schema (§1), so an
extracted schema can be edited and fed straight back in. Without it, it is a
spec.

### 2.1 Top level

| Key | Required | Meaning |
|---|---|---|
| `fields` | **yes** | Array of field entries. A spec without it is refused. |
| `name` | no | Defaults to `"form"`. |
| `screens` | no | Array of `{"id", "title"}` objects **or bare strings** (a string becomes a screen with that id and no title). A screen a field names but the list does not is added at the end. |

The resulting schema's `source` is `{"kind": "spec", "path": <file>}`.

### 2.2 How a spec field differs from a schema field

Only `name` is required; a field without it is refused. Everything else
differs from the schema in these ways:

| In a spec | Becomes in the schema |
|---|---|
| Constraint keys written flat on the field: `required`, `min_length`, `max_length`, `pattern`, `minimum`, `maximum`, `step`, `multiple`, `read_only` | Moved into `constraints`. A nested `constraints` object is also accepted; a flat key wins over the same key in it. |
| `options` as bare strings: `["CA", "NY"]` | `[{"value": "CA", "label": "CA"}, …]`. Objects are accepted too. |
| `type` | Read as `control` when `control` is absent. |
| `semantic_type` given | Taken as given: `confidence` 1.0, `evidence` `["declared in field spec"]`, and inference does not overrule it. |
| `semantic_type` absent | Inferred exactly as from HTML. `data_type`, `confidence` and `evidence` are always inference's. |
| `follows`: a field name or an array of them | `derived.sources` |
| `when`: an object from a key to one value or an array of values | `derived.table`. Keys with several sources join the values with `\|` in `follows` order; case and surrounding space are ignored. A single value becomes a one-element array. |
| `otherwise`: one value or an array | `derived.otherwise` |
| `derived` (the schema's own spelling) | Used as it is, and `follows`/`when` on the same field are then ignored. |

`when` without `follows` is ignored. Keys the spec does not know are dropped;
`extra` passes through.

### 2.3 Example

Four fields of `examples/patient_registration.fields.json`:

```json
{"name": "patient_postal_code", "label": "ZIP", "screen": "patient",
 "group": "patient", "required": true, "pattern": "\\d{5}"}

{"name": "relationship_to_insured", "label": "Relationship to Insured",
 "screen": "insurance", "control": "select", "required": true,
 "options": ["Self", "Spouse", "Child", "Other"]}

{"name": "copay_amount", "label": "Copay", "screen": "insurance",
 "control": "number", "minimum": 0, "maximum": 250, "step": 5}

{"name": "consent_to_treat", "label": "I consent to treatment",
 "screen": "visit", "control": "checkbox", "required": true}
```

and the same fields after `fillerai extract`:

```json
{"name": "patient_postal_code", "label": "ZIP", "semantic_type": "postal_code",
 "data_type": "string", "control": "text", "screen": "patient", "group": "patient",
 "constraints": {"required": true, "pattern": "\\d{5}"}, "confidence": 0.8,
 "evidence": ["name matches /(zip|postal|postcode|post_?code)/"]}

{"name": "relationship_to_insured", "label": "Relationship to Insured",
 "semantic_type": "enum", "data_type": "string", "control": "select",
 "screen": "insurance",
 "options": [{"value": "Self", "label": "Self"}, {"value": "Spouse", "label": "Spouse"},
             {"value": "Child", "label": "Child"}, {"value": "Other", "label": "Other"}],
 "constraints": {"required": true}, "confidence": 0.72,
 "evidence": ["closed set of options, no other signal"]}

{"name": "copay_amount", "label": "Copay", "semantic_type": "currency_amount",
 "data_type": "number", "control": "number", "screen": "insurance",
 "constraints": {"minimum": 0, "maximum": 250, "step": 5}, "confidence": 0.855,
 "evidence": ["name matches /(amount|price|cost|salary|income|balance|premium|deductible|total|fee|(coverage|credit|policy|spending)_?limit)/",
              "refines decimal", "input type=number"]}

{"name": "consent_to_treat", "label": "I consent to treatment",
 "semantic_type": "boolean", "data_type": "boolean", "control": "checkbox",
 "screen": "visit", "constraints": {"required": true}, "confidence": 0.9,
 "evidence": ["input type=checkbox"]}
```

A declared rule, from `examples/auto_insurance_quote.fields.json`:

```json
{"name": "sr22_required", "label": "SR-22 Filing Required", "screen": "driver",
 "control": "select", "options": ["Yes", "No"],
 "follows": "license_status",
 "when": {"Valid": "No", "Restricted": "Yes", "Suspended": "Yes"}}
```

which is the `derived` block shown in §1.6.

---

## 3. Datasets

A dataset is a list of **records**. A record is a JSON object from field
`name` to value. `fillerai generate -f json|ndjson|csv` writes one
(`Dataset.render`); `fillerai train` and `fillerai evaluate` read any of the
three back, chosen by file extension (`_load_records` in `cli.py`).

### 3.1 Values

| Case | In JSON / NDJSON | In CSV |
|---|---|---|
| An ordinary value | a string — numbers, dates and amounts are formatted strings (`"35.00"`, `"2027-01-25"`) | the string |
| A checkbox (a `boolean` field with control `checkbox` and no options) | `true` / `false` | `true` / `false` |
| An optional field left empty by the blank rate | `""` | empty cell |
| A `read_only` field | **the key is absent** | empty cell |
| A list (not produced by the generator today) | an array | the items joined with `\|` |
| `null` | `null` | empty cell |

A field with options always holds one of its option **values**, not labels.
Records keep the form's own field order, whatever order the generator
resolved them in.

With `--include-persona`, each record gains a `_persona` key:
`{"name", "date_of_birth", "addresses": {group or "default": "street, city, ST zip"}}`.
It appears in JSON and NDJSON only; CSV writes schema columns and drops it.

### 3.2 The three layouts

| Format | Extension read back | Shape |
|---|---|---|
| `json` (default) | `.json` | `{"form": <schema name>, "count": <n>, "records": [ … ]}`, indented 2. On reading, a bare array of records is also accepted. |
| `ndjson` | `.ndjson` | One record per line, no wrapper. Blank lines are skipped on reading. |
| `csv` | `.csv` | A header row of every field `name` in schema order, then one row per record. Standard `csv` module quoting. |

Reading a CSV gives every value back as a string, so `true` and `"true"` are
no longer different. The model normalises values before counting them, so a
CSV dataset trains the same as the JSON it came from.

In the library a dataset's payload is the bare array of records, without the
`form`/`count` wrapper (§5.4).

### 3.3 Example

`fillerai generate examples/patient_registration.fields.json -n 2 --seed 7`
with each format (one record shown).

CSV:

```
patient_first_name,patient_last_name,patient_date_of_birth,patient_email,patient_phone,patient_street,patient_city,patient_state,patient_postal_code,insured_member_id,insured_group_number,insured_policy_number,insured_company,relationship_to_insured,copay_amount,visit_date,visit_time,diagnosis_code,reason_for_visit,referring_provider,consent_to_treat
Tomas,Silva,1999-06-14,tomas.silva@test.example,(602) 555-0180,31 Market St,Phoenix,AZ,85003,ID2037872,GRP61993,POL-331821,,Spouse,35.00,2027-01-25,09:15,J45.909,Additional documentation was requested and is pending review. Customer reported the issue during the scheduled follow-up call. No prior claims were found under this policy number.,Christopher Mitchell,true
```

NDJSON (one line; wrapped here):

```json
{"patient_first_name": "Tomas", "patient_last_name": "Silva", "patient_date_of_birth": "1999-06-14",
 "patient_email": "tomas.silva@test.example", "patient_phone": "(602) 555-0180",
 "patient_street": "31 Market St", "patient_city": "Phoenix", "patient_state": "AZ",
 "patient_postal_code": "85003", "insured_member_id": "ID2037872", "insured_group_number": "GRP61993",
 "insured_policy_number": "POL-331821", "insured_company": "", "relationship_to_insured": "Spouse",
 "copay_amount": "35.00", "visit_date": "2027-01-25", "visit_time": "09:15", "diagnosis_code": "J45.909",
 "reason_for_visit": "Additional documentation was requested and is pending review. …",
 "referring_provider": "Christopher Mitchell", "consent_to_treat": true}
```

JSON:

```json
{
  "form": "patient_registration",
  "count": 2,
  "records": [
    {"patient_first_name": "Tomas", "patient_last_name": "Silva", "…": "…", "consent_to_treat": true},
    {"patient_first_name": "Wei", "patient_last_name": "White", "…": "…", "consent_to_treat": true}
  ]
}
```

`insured_company` was left empty by the blank rate, and shows as `""` and as
an empty cell.

---

## 4. The trained model

`AutofillModel.to_dict()` / `to_json()`. `fillerai train -o FILE` writes it
(indented 2); the library keeps the same object as a model's payload. The
file is self-contained: it embeds the schema it was trained for.

### 4.1 Top level

| Key | Type | Meaning |
|---|---|---|
| `model_version` | string | `MODEL_VERSION`, currently `"2.0"`. |
| `algorithm` | string | The engine: `statistical`, `tree`, `forest`, `nearest`, `bayes` or `linear`. |
| `schema` | object | The full field schema (§1). |
| `trained_on` | integer | Records learned from. |
| `held_out` | integer | Records kept back to calibrate and score. |
| `settings` | object | What the run was asked for: `algorithm`, `holdout`, `seed`, `tuning` (the per-algorithm `--set` values), `use_rules`, `learn_weights`. |
| `profiles` | array of Profile | One per field, §4.2. |
| `engine` | object | The engine's own state, §4.4. |
| `derivations` | array of Derivation | Verified rules, §4.3. |
| `calibration` | object | `fitted` (boolean), `bins` (ten `[attempts, hits]` pairs, one per tenth of raw score), `rates` (ten calibrated confidences). |
| `combiner` | object | Vote weights: `fitted`, `features` (`["heuristic", "strength", "support", "peak", "floor"]`), `weights`, `bias`, `votes`, `loss`, `baseline_loss`, `accuracy`, `baseline_accuracy`. A saved combiner whose `features` differ from this build's is not loaded; the hand-picked weights are used instead. |

### 4.2 Profile

What one column of the training data looked like.

| Key | Written | Meaning |
|---|---|---|
| `name`, `semantic_type` | always | From the schema. |
| `rows`, `filled`, `distinct` | always | Records seen, records with a value, distinct values. |
| `kind` | always | `constant`, `enumerable` or `open`. |
| `group`, `screen` | when set | From the schema. |
| `values` | when non-empty | `[value, count]` pairs. |
| `format` | when set | The shape of the values, for `open` fields only. |
| `closed` | when true | The form limits the field to its options. |

### 4.3 Derivation

`{"kind", "target", "inputs", "params", "accuracy", "support"}`. A rule is
kept only at `accuracy` ≥ 0.9 over `support` ≥ 5 records. Kinds and their
`params`, from real runs on `examples/`:

| `kind` | `params` | Example |
|---|---|---|
| `copy` | none | `{"kind": "copy", "target": "garaging_state", "inputs": ["license_state"], "params": {}, "accuracy": 1.0, "support": 150}` |
| `join` | `template`, e.g. `"{0} {1} {2}"` | `{"kind": "join", "target": "member_full_name", "inputs": ["member_first_name", "member_middle_name", "member_last_name"], "params": {"template": "{0} {1} {2}"}, "accuracy": 0.9615, "support": 26}` |
| `initial` | `upper`, `suffix` | `{"kind": "initial", "target": "member_middle_initial", "inputs": ["member_middle_name"], "params": {"upper": true, "suffix": ""}, …}` |
| `email` | `template`, `domain` | |
| `age` | `as_of` (a date) | `{"kind": "age", "target": "member_age", "inputs": ["member_date_of_birth"], "params": {"as_of": "2026-09-27"}, …}` |

### 4.4 What each engine stores

`engine` always has `algorithm` (the linear engine's `Weights` entries do
not). The rest is per engine:

| Engine | Keys in `engine` | Contents |
|---|---|---|
| `statistical` | `links` | One **Link** per (source, target) pair: `source`, `target`, `strength` (lambda), `support`, and `table`, which maps a source value to `[rows, [[target value, count], …]]`. |
| `tree` | `label` (`"tree"`), `trees` | `trees` maps a target field to an array of one **Tree**: `root`, `importance` (field → share), `strength`, `accuracy`, `tested`. A **Node** is `rows`, `p` (`[value, probability]` pairs), and, when it splits, `on` (the field it asks) and `kids` (value → Node). |
| `forest` | `label` (`"forest"`), `trees` | The same layout as `tree`, with ten trees per target by default. `algorithm` is `"forest"`. |
| `nearest` | `fields`, `rows`, `weights`, `surprise`, `neighbours`, `strengths` | A sample of past records as arrays of strings in `fields` order; `weights` is target → source → relevance; `surprise` is field → value → −log2(share); `neighbours` defaults to 12. |
| `bayes` | `priors`, `tables`, `totals`, `strengths` | `priors`: target → value → share. `tables`: target → source → target value → source value → count. `totals`: target → value → rows counted. |
| `linear` | `buckets`, `hashed`, `weights` | `buckets` defaults to 4096; `hashed` lists fields whose values are bucketed rather than named; `weights` maps a target to `{classes, bias, rows, strength, tested, epochs, importance}`, where `rows` maps a token (`field=value`, or a bucket) to one weight per class. |

Sizes differ a great deal. On 200 records of `claims_intake.html` the files
were 40 KB (`statistical`), 37 KB (`tree`), 121 KB (`forest`), 65 KB
(`nearest`), 211 KB (`bayes`) and 1.2 MB (`linear`).

### 4.5 Example

`fillerai train examples/patient_registration.fields.json <300 records> --seed 1 -o pr.model.json`,
long arrays cut short:

```json
{
  "model_version": "2.0",
  "algorithm": "statistical",
  "schema": {"schema_version": "1.0", "name": "patient_registration", "…": "…"},
  "trained_on": 225,
  "held_out": 75,
  "settings": {"algorithm": "statistical", "holdout": 0.25, "seed": 1,
               "tuning": {}, "use_rules": true, "learn_weights": true},
  "profiles": [
    {"name": "patient_first_name", "semantic_type": "first_name", "rows": 225,
     "filled": 225, "distinct": 71, "kind": "enumerable", "group": "patient",
     "screen": "patient", "values": [["Elizabeth", 9], ["Cameron", 7], "…"]},
    {"name": "consent_to_treat", "semantic_type": "boolean", "rows": 225,
     "filled": 225, "distinct": 1, "kind": "constant", "screen": "visit",
     "values": [["true", 225]]}
  ],
  "engine": {
    "algorithm": "statistical",
    "links": [
      {"source": "patient_postal_code", "target": "patient_city",
       "strength": 0.9571, "support": 225,
       "table": {"62701": [6, [["Springfield", 6]]], "11215": [5, [["Brooklyn", 5]]], "…": "…"}}
    ]
  },
  "derivations": [],
  "calibration": {"fitted": true,
                  "bins": [[0, 0], [6, 1], [363, 59], [28, 18], [10, 10], [16, 16], [42, 42], [87, 87], [31, 31], [302, 302]],
                  "rates": [0.05, 0.1591, 0.1637, 0.5985, 0.8167, 0.8929, 0.9628, 0.9844, 0.9844, 0.9992]},
  "combiner": {"fitted": false, "features": ["heuristic", "strength", "support", "peak", "floor"],
               "weights": [0.0, 0.0, 0.0, 0.0, 0.0], "bias": 0.0, "votes": 0, "loss": 0.0,
               "baseline_loss": 0.0, "accuracy": 0.0, "baseline_accuracy": 0.0}
}
```

### 4.6 Version and compatibility

- `MODEL_VERSION` is `"2.0"`. It went from `"1.0"` when the engine became
  selectable and moved under its own `engine` key.
- `from_dict` accepts major `1` and major `2`, and refuses any other with a
  `ValueError` naming both versions.
- A **1.x** file has its links at the top level (`links`), no `algorithm` and
  no `engine`. It is read as `statistical` with those links, which is all the
  migration there is. A loaded model always reports `model_version` `"2.0"`
  and is written back in the 2.0 layout.
- The embedded `schema` is read by `FormSchema.from_dict`, so the schema
  rules in §1.7 apply to it as well.

---

## 5. The library

Every stage can keep what it produced, with a pointer to what it was made
from. There are two implementations of one interface: a directory
(`store.Store`, the CLI's default) and the database (`dbstore.DatabaseStore`,
what the server uses with accounts). A caller cannot tell which it has. The
reasons are in [architecture.md §4](../architecture.md#4-the-library--storepy-dbstorepy).

### 5.1 Kinds and ids

`KINDS` in `store.py`, six of them:

| Kind | Id prefix | Folder | Payload |
|---|---|---|---|
| `source` | `src-` | `sources/` | `{"content": <the HTML or spec text>, "kind": "html" \| "spec"}` |
| `schema` | `sch-` | `schemas/` | A field schema (§1). |
| `dataset` | `dat-` | `datasets/` | An array of records (§3), without the JSON wrapper. |
| `model` | `mdl-` | `models/` | A trained model (§4). |
| `script` | `scr-` | `scripts/` | `{"text": <Python source>}`: the training script for the model it hangs off. |
| `template` | `tpl-` | `templates/` | A bot template (§8.1). |

An id is `<prefix>-<YYYYMMDD>-<HHMMSSmmm>-<4 hex>`, for example
`mdl-20260927-143406284-9371`. The time is local and to the millisecond;
within one process each new id is pushed at least a millisecond past the last
one, so ids sort in the order they were made. The file library accepts only
ids matching `^(src|sch|dat|mdl|scr|tpl)-\d{8}-\d{9}-[0-9a-f]{4}$`, because
an id becomes a filename. Listings are newest first, sorted on the time part
of the id (characters 4 to 22), not on the whole id.

### 5.2 The entry (manifest)

`store.Entry`, the same in both implementations.

| Key | Type | Meaning |
|---|---|---|
| `id` | string | As above. |
| `kind` | string | One of `KINDS`. |
| `name` | string | For people. Defaults to the id. The only thing that can change after writing (`rename`). |
| `created` | string | ISO 8601 with offset, to the second. |
| `parent` | string or null | The id this was made from. Must exist when the entry is written. |
| `meta` | object | Whatever the writer thought worth listing. Free-form. |
| `bytes` | integer | Size of the payload JSON in UTF-8. |

What the writers put in `meta`:

| Kind | `meta` |
|---|---|
| `source` | `kind` (`html`/`spec`), `characters` |
| `schema` | `fields`, `screens` |
| `dataset` | `records`; `fillerai generate --save` adds `seed`, `blank_rate` |
| `model` | `algorithm`, `trained_on`, `held_out`, `rules`; the CLI adds `seeds`, the server adds `seeds` and its scores |
| `script` | `lines`; the server adds `algorithm` |
| `template` | `key`, `fields`, `model_id` |

### 5.3 Lineage

`parent` builds the chain `source → schema → dataset → model → script`.
`lineage(id)` walks up to the root (oldest first); `children` and
`descendants` walk down.

| Kind | Parent |
|---|---|
| `source` | none |
| `schema` | its `source`, or none when a stage saved the schema without one |
| `dataset` | its `schema` (required) |
| `model` | its `dataset` (required) |
| `script` | its `model` |
| `template` | the `schema` it was started from, or none. A parent that no longer exists is dropped rather than refused. |

`fillerai train --save` without `--from-dataset` writes the schema and the
dataset too, so a model never has a lineage that stops at itself.

### 5.4 Immutability and deletion

- **A payload is never rewritten.** There is no update; a changed thing is a
  new entry. Only `name` can change. This is what lets `/v1` cache a loaded
  model per `(owner, entry id)` without it going stale.
- **Templates replace by key.** Saving a template whose `key` already exists
  writes a new `tpl-` entry and then deletes the old one, so the key keeps
  meaning "the current version" while each entry stays unchanged.
- **Delete refuses to orphan.** An entry with children cannot be deleted
  without `cascade`, except that `script` children go with their parent.
  `cascade` removes every descendant.
- **Prune** (`keep=50`) drops the oldest entries of each kind past `keep`,
  but only entries with no children.

### 5.5 The directory library

The root is `$FILLERAI_HOME`, else `./.fillerai`. One folder per kind, two
files per entry, both JSON indented 1:

```
.fillerai/
  sources/   src-20260927-143405678-a95c.json            the entry (§5.2)
             src-20260927-143405678-a95c.payload.json    the payload
  schemas/   sch-20260927-143405679-3fff.json
             sch-20260927-143405679-3fff.payload.json
  datasets/  dat-20260927-143405888-f988.json …
  models/    mdl-20260927-143406284-9371.json …
  scripts/   (written by the server's training run)
  templates/ tpl-20260927-143454658-36ed.json …
  fillerai.db                                           the default database (§6)
```

A listing reads only the small `<id>.json` files. A file that cannot be read
is skipped, so an interrupted write costs one entry, not the library. There is
no index, no lock and no owner.

The entry file of the model above:

```json
{
 "id": "mdl-20260927-143406284-9371",
 "kind": "model",
 "name": "patient_registration: statistical",
 "created": "2026-09-27T14:34:06+00:00",
 "parent": "dat-20260927-143405888-f988",
 "meta": {
  "algorithm": "statistical",
  "trained_on": 225,
  "held_out": 75,
  "rules": 0,
  "seeds": ["patient_postal_code", "patient_city", "insured_company"]
 },
 "bytes": 49870
}
```

### 5.6 The database library

The same entries in two tables, `entries` and `payloads` (§6.2), with an
`owner` column on both. An entry is named by `(id, owner)`: two owners can
hold the same id and neither can reach the other's. `owner` is the user's
`id` (`usr-…`), or `""` for the unowned library a server started with
`--no-auth` uses. `meta` is stored as a JSON string; the payload's `body` is
the same JSON text the file library would write. An entry and its payload are
written in one transaction.

`fillerai db import [--from DIR] [--user NAME]` copies a directory library in,
oldest first so parents arrive before children. Ids are kept, so running it
twice copies nothing the second time. A parent that did not come across
becomes `null` rather than a dangling id.

---

## 6. The database

`fillerai/db.py`. SQLite from the standard library. Why it is shaped this way
is in [architecture.md §5](../architecture.md#5-storage--dbpy).

### 6.1 Where it is

| Setting | Value |
|---|---|
| URL variable | `FILLERAI_DATABASE_URL` |
| Default | `sqlite://<library root>/fillerai.db`, the library root being `./.fillerai` unless given |
| Accepted URLs | `sqlite://<path>`, `sqlite://:memory:`, or a bare filesystem path. `postgres://` and `postgresql://` are recognised and refused with the reason (no driver). |
| Connection pragmas | `foreign_keys = ON`, `busy_timeout = 10000`, `journal_mode = WAL` (files only), `synchronous = NORMAL` |

Every column is `TEXT` or `INTEGER`. Times are ISO 8601 strings. Booleans are
`INTEGER` 0 or 1. There are no triggers, views, foreign keys or defaults
beyond `NULL`.

### 6.2 Migrations

`MIGRATIONS` is a list of `(version, [statements])`. `migrate()` runs on
every connect: it makes the bookkeeping table if it is missing, then applies
each step whose version is not yet recorded, each in one transaction, and
records it.

**A step that has shipped is never edited. A change is a new step.** A
database made by an older build catches up on start rather than being
recreated.

| Step | Adds |
|---|---|
| — | `schema_version` (created by `migrate()` itself, not a step) |
| 1 | `users`, `sessions` + index `sessions_by_user`, `entries` + indexes `entries_by_owner`, `entries_by_parent`, `payloads`, `settings` |
| 2 | `api_tokens` + index `tokens_by_user` |

`schema_version` here is the database's migration record and has nothing to
do with the field schema's `schema_version` key (§1).

`fillerai db status` prints the version and table count:

```
  SQLiteDatabase at sqlite:///…/fa.db
  schema version 2, 7 table(s)
  1 user(s), 1 administrator(s)
  4 library entr(ies) across 1 owner(s)
```

### 6.3 Tables

**`schema_version`**

| Column | Type | Meaning |
|---|---|---|
| `version` | INTEGER, primary key | A migration step applied. |
| `applied` | TEXT not null | When. |

**`users`** (step 1)

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT, primary key | `usr-` + 16 hex. |
| `username` | TEXT not null, unique | Lower case, 2 to 32 of `a-z 0-9 . _ -`, starting with a letter or digit. |
| `display_name` | TEXT not null | Up to 80 characters. |
| `role` | TEXT not null | `admin` or `user`. |
| `active` | INTEGER not null | 0 or 1. |
| `password_hash` | TEXT not null | §7.1. |
| `must_change` | INTEGER not null | Ask for a new password at next sign-in. |
| `created` | TEXT not null | |
| `last_login` | TEXT | |
| `failures` | INTEGER not null | Failed sign-ins since the last success or lockout. |
| `locked_until` | TEXT | Set for 15 minutes after 6 failures. |

**`sessions`** (step 1), index `sessions_by_user (user_id)`

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT, primary key | `secrets.token_urlsafe(24)`. |
| `user_id` | TEXT not null | |
| `csrf` | TEXT not null | `secrets.token_urlsafe(24)`; sent back in a header on `/api` calls. |
| `created` | TEXT not null | A session never outlives `created` + 7 days. |
| `last_seen` | TEXT not null | |
| `expires` | TEXT not null | `last_seen` + 12 hours, moved on each use. |
| `agent` | TEXT | The browser's user agent, up to 200 characters. |

**`entries`** (step 1), primary key `(id, owner)`, indexes
`entries_by_owner (owner, kind)` and `entries_by_parent (parent)`

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT not null | Library id (§5.1). |
| `owner` | TEXT not null | `users.id`, or `""`. |
| `kind` | TEXT not null | |
| `name` | TEXT not null | |
| `created` | TEXT not null | |
| `parent` | TEXT | |
| `meta` | TEXT not null | JSON. |
| `bytes` | INTEGER not null | |

**`payloads`** (step 1), primary key `(entry_id, owner)`

| Column | Type | Meaning |
|---|---|---|
| `entry_id` | TEXT not null | |
| `owner` | TEXT not null | |
| `body` | TEXT not null | The payload JSON. Kept apart from `entries` so a listing never reads it. |

**`settings`** (step 1)

| Column | Type | Meaning |
|---|---|---|
| `name` | TEXT, primary key | |
| `value` | TEXT not null | |

The one setting written today is `session_secret`: 32 random bytes, base64,
made on first use and used to sign session cookies (§7.2). Deleting the
database therefore signs everybody out.

**`api_tokens`** (step 2), index `tokens_by_user (user_id)`

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT, primary key | 12 hex characters; the middle part of the token string. |
| `user_id` | TEXT not null | Whose library the token reads. |
| `name` | TEXT not null | A label, up to 60 characters; `integration` when none is given. |
| `secret_hash` | TEXT not null | §7.3. |
| `created` | TEXT not null | |
| `last_used` | TEXT | Set when the caller chooses to record a use. |
| `expires` | TEXT | Null means it does not expire. |
| `model_id` | TEXT | Limits the token to one model, or null for the whole library. |
| `revoked` | INTEGER not null | 0 or 1. A revoked token stays as a row. |

Deleting a user deletes, in one transaction, their `payloads`, `entries`,
`sessions`, `api_tokens` and the `users` row.

---

## 7. Passwords, sessions and API tokens

Why a password and a token are hashed differently is in
[architecture.md §6](../architecture.md#6-who-is-asking--authpy-tokenspy-webkeyringpy).
This section is only the formats.

### 7.1 Password hashes

`auth.hash_password`. The hash carries its own parameters, separated by `$`,
with a random 16-byte salt. Salt and digest are standard base64.

| Format | Layout | Parameters |
|---|---|---|
| scrypt (normal) | `scrypt$<n>$<r>$<p>$<salt>$<digest>` | n = 16384, r = 8, p = 1, 32-byte digest |
| PBKDF2 (fallback, only when Python lacks scrypt) | `pbkdf2_sha256$<rounds>$<salt>$<digest>` | 600,000 rounds of HMAC-SHA256 |

Both are verified, whichever build wrote them, with a constant-time compare.
A hash in neither format fails verification rather than raising. Real
example, cut short:

```
scrypt$16384$8$1$+ZNqd2qz2DDlewnrokuiOw==$F4wARtDqIxqesvPI1R…
```

Password rules: at least 8 characters, at most 1024, and not the username.

### 7.2 Session cookie

Cookie name `fillerai_session`. Value `<session id>.<signature>`, where the
signature is the first 32 hex characters of HMAC-SHA256 over the session id,
keyed by the `session_secret` setting. The session itself is the `sessions`
row; the cookie holds nothing else.

### 7.3 API tokens

The string an application holds:

```
flr_<id>_<secret>
flr_e2f33bc59dd3_q4w4O34i_cViblULJfKVS0nuVSqg9h8Y
```

| Part | Format |
|---|---|
| `flr` | Fixed prefix, so a leaked token is recognisable. |
| `<id>` | 12 lower-case hex characters (`secrets.token_hex(6)`); the `api_tokens.id`. |
| `<secret>` | `secrets.token_urlsafe(24)`: 24 random bytes, 32 URL-safe base64 characters. |

A presented token must match `^flr_([0-9a-f]{12})_([A-Za-z0-9_-]{16,64})$`.

What is stored is `api_tokens.secret_hash`: the **SHA-256 hex digest of the
`<secret>` part only**, not of the whole string. The secret itself is never
stored and is shown once, by `fillerai tokens add` or the UI. For the token
above the row is:

```
id           e2f33bc59dd3
user_id      usr-412902b098554dfd
name         claims-system
secret_hash  de2672670656f5ae092b49d528038c600aab26417cc7664d9bb26da37bee722c
created      2026-09-27T14:34:43+00:00
last_used    NULL
expires      2026-12-26T14:34:43+00:00
model_id     NULL
revoked      0
```

Verifying is one lookup by `id` and a constant-time compare of digests. Every
failure (malformed, unknown id, wrong secret, revoked) gives the same answer;
only expiry is reported separately. A listing shows `prefix` (`flr_<id>`),
never the secret. An account holds at most 25 live tokens.

---

## 8. Bot templates and conversation state

What a template is for, and how a turn uses it, is in
[bot-builder.md](../bot-builder.md). This section is the field list.

### 8.1 Template

`bot.template.Template.to_dict()`. Kept in the library as kind `template`
(§5.1), sent and received by `/v1/templates` and the Bots tab, and shipped as
the two starters in `fillerai/bot/starters/*.template.json`
(`address_change`, `document_request`).

| Key | Type | Meaning |
|---|---|---|
| `template_version` | string | `TEMPLATE_VERSION`, currently `"1.0"`. Same major-only rule as the field schema. Always written as the current version. |
| `key` | string | **The stable name.** `^[a-z][a-z0-9_]{0,63}$`. Saving a template with an existing key replaces it (§5.4). |
| `name` | string | Defaults to the key, capitalised, with spaces. |
| `description` | string | |
| `examples` | array of string | Phrasings that mean this request. At most 100. |
| `fields` | array of TemplateField | At least one, at most 200. |
| `actions` | array of string | One or both of `fill_form`, `submit`, written in that order. Defaults to both. |
| `model_id` | string or null | A model (`mdl-…`) whose field names match, used to complete related fields. |
| `done_message` | string | Said when the conversation finishes. |

Every text value has its whitespace collapsed and may be at most 500
characters. `entry_id` (the library id it was read from) is accepted on input
but never written into the template.

### 8.2 Template field

| Key | Type | Written | Meaning |
|---|---|---|---|
| `name` | string | always | The join key, as in the schema. `^[A-Za-z_][A-Za-z0-9_.-]{0,99}$`. |
| `label` | string | always | Defaults to the name, capitalised, with spaces. |
| `semantic_type` | string | always | **Must** be one of the §1.3 types. Defaults to `unknown`. |
| `required` | boolean | always | |
| `aliases` | array of string | when non-empty | Other words for the field. At most 30. |
| `options` | array of string | when non-empty | A closed list. At most 500. On input an option may also be a schema-style `{"value", "label"}` object; its `value` is kept. |
| `example` | string | when set | Shown in a typed question. |
| `follows` | array of string | when non-empty | Other fields of this template this one depends on. |

A template's `follows` is not the field spec's `follows`. In a spec it names
the sources of a rule with a `when` table (§2.2). In a template it has no
table: it means that when one of the named fields changes during the
conversation, the value held for this one is dropped and asked for again.

### 8.3 Validation errors

`Template.from_dict` refuses a template rather than half-loading one. It
raises `TemplateError` (a `ValueError`) for:

| Problem | Message begins |
|---|---|
| Not a JSON object | `a template must be a JSON object` |
| Major version differs | `this template is version …; this build reads 1.0` |
| Bad `key` | `a template needs a key of lower-case letters, digits and _, starting with a letter` |
| `fields` missing, empty or not an array | `a template needs at least one field` |
| More than 200 fields | `a template may have at most 200 fields` |
| A field is not an object | `field N must be an object` |
| Bad field `name` | `field N needs a name of letters, digits and _` |
| Unknown `semantic_type` | `<name>: '<type>' is not a semantic type` |
| `aliases` / `options` / `follows` / `examples` not an array, or too long | `<where> must be a list` / `<where> may have at most N entries` |
| Text over 500 characters | `'…'... is longer than 500 characters` |
| Two fields with one name | `two fields are called '<name>'` |
| `follows` names a field not in the template, or itself | `<name> follows '<other>', which is not another field here` |
| `actions` empty or with anything but the two actions | `actions must list one or both of fill_form, submit` |
| `model_id` not starting `mdl-` | `'<id>' is not a model id` |

`from_schema` (starting a template from a library schema) raises
`none of that schema's fields can go in a template` when every field is a
password, free text, read-only, or has a name the pattern rejects.

### 8.4 Example

`bot/starters/address_change.template.json`, as saved by
`fillerai bot add --starter address_change` (three of six fields shown):

```json
{
  "template_version": "1.0",
  "key": "address_change",
  "name": "Address change",
  "description": "Update the mailing address on file",
  "examples": ["update my address", "change my address", "I moved", "…"],
  "fields": [
    {"name": "city", "label": "City", "semantic_type": "city",
     "required": true, "example": "Irvine"},
    {"name": "postal_code", "label": "ZIP code", "semantic_type": "postal_code",
     "required": true, "aliases": ["zip", "zipcode", "postcode"],
     "example": "92618", "follows": ["street_address", "city", "state"]},
    {"name": "country", "label": "Country", "semantic_type": "country",
     "required": false, "options": ["US", "CA", "MX"]}
  ],
  "actions": ["fill_form", "submit"],
  "model_id": null,
  "done_message": "Your address change is in."
}
```

Its library entry has `meta` `{"key": "address_change", "fields": 6, "model_id": null}`.

### 8.5 Conversation state

`bot.conversation.State.to_dict()`. The service keeps no conversation: each
reply carries `state`, the client sends it back unchanged with the next
turn, and `read_state` checks it again from scratch because it has been
through a browser ([bot-builder.md §4](../bot-builder.md#4-turns-without-a-session-on-the-server)).
The client does not need to read it.

| Key | Type | Meaning |
|---|---|---|
| `v` | integer | `STATE_VERSION`, currently `1`. Any other value is refused. |
| `id` | string | `cnv-` + 12 hex, at most 40 characters. Kept when a finished conversation starts again. |
| `turn` | integer | Incremented each turn. |
| `status` | string | `idle`, `collecting`, `ready`, `submitting`, `handed_off`, `done` or `cancelled`. The last three are finished. |
| `template` | string or null | The key of the template in progress. Must be one this chat may use. |
| `values` | object | Field name → **Value**: `{"value", "source", "confidence", "said_before"?}`. `source` is `said`, `clicked` or `model`; `said_before` (up to 200 characters) is the old value the person mentioned. Every value is re-checked against its field. |
| `expects` | string or null | The field the last question asked for. |
| `last_field` | string or null | The field last filled. |
| `offered` | array of string | Action ids offered last turn, at most 40. An action not in this list is refused as `stale_action`. |
| `pending` | string | What was said before the person picked a template, read again after. |
| `parked` | object | Template key → `values`, for templates set aside by a change of subject. Entries for templates this chat may not use are dropped. |

A state that fails any check is refused with code `bad_state` (or
`template_not_allowed` when the template is not available). After
"I moved to 12 Oak St, Irvine CA" and then "92618":

```json
{
  "v": 1,
  "id": "cnv-2e9378a3dc54",
  "turn": 2,
  "status": "ready",
  "template": "address_change",
  "values": {
    "state": {"value": "CA", "source": "said", "confidence": 0.75},
    "street_address": {"value": "12 Oak St", "source": "said", "confidence": 0.75},
    "city": {"value": "Irvine", "source": "said", "confidence": 0.7},
    "postal_code": {"value": "92618", "source": "said", "confidence": 0.9}
  },
  "expects": null,
  "last_field": "postal_code",
  "offered": ["submit", "fill_form", "cancel"],
  "pending": "",
  "parked": {}
}
```

The rest of a turn's request and reply is documented in
[bot-builder.md §3](../bot-builder.md#3-one-turn).

---

## 9. Version numbers at a glance

| Format | Constant | Current | Compatibility rule |
|---|---|---|---|
| Field schema | `SCHEMA_VERSION` (`schema.py`) | `"1.0"` | Same major only; unknown keys ignored. |
| Trained model | `MODEL_VERSION` (`train/model.py`) | `"2.0"` | Majors 1 and 2 load; 1.x read as `statistical`. |
| Bot template | `TEMPLATE_VERSION` (`bot/template.py`) | `"1.0"` | Same major only. |
| Conversation state | `STATE_VERSION` (`bot/conversation.py`) | `1` | Exact match only. |
| Database | `MIGRATIONS` (`db.py`) | step 2 | Forward only; shipped steps never edited. |
| Field spec, dataset, library entry | — | unversioned | Readers ignore unknown keys. |
