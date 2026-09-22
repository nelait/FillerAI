# Assumptions

Everything FillerAI takes as given. Some are constraints somebody chose, some
are beliefs about forms that happen to hold, and some are constants that a
stopwatch would replace. They are gathered here because each one is a place
the system can be wrong, and a number produced under an assumption nobody
wrote down is a number nobody can argue with.

Each entry says what is assumed, why, where it lives, and what breaks if it is
false.

---

## 1. About the environment this runs in

### 1.1 The real form data cannot leave the environment it lives in

This is the founding assumption and every other constraint here descends from
it. A claims operation will not hand over its submitted records, so the tool
has to work without them.

**Consequences:** the generate stage exists at all; the training stage is on
the other side of the schema from it, so real records can be substituted
without touching anything; `fillerai/generate/catalogs.py` contains only
public reference data; the LLM features are fenced, off by default, and the
one that touches a network is a single method.

**If it is false** — if someone *can* share their records — most of this is
still useful and the generate stage becomes optional. That is the good case,
and §8 of [process.md](process.md) is the argument for chasing it.

### 1.2 Nothing can be installed where this runs

Locked-down environments do not get `pip install`. So: `dependencies = []` in
`pyproject.toml`, asserted by `tests/test_llm_fence.py`; no build step for the
UI; SQLite rather than a server; `http.server` rather than a framework; every
algorithm in plain Python, including the decision trees and the softmax
regression.

**What it costs**, plainly: there is no scikit-learn, no numpy, no Postgres
driver, and no npm package for the browser client. The engines are slower than
a compiled one by a wide margin, and [weight-based-training.md](weight-based-training.md)
§2 measures exactly how much.

**If it is false** for a given deployment, the interfaces are already in the
right places — `Database` for Postgres, `Transport` for a first-party SDK —
but the rule does not get relaxed by default.

### 1.3 It is a local working tool, not a public service

`serve` binds to loopback and says so loudly if asked to do otherwise.
`--no-auth` is localhost-only, since anyone who can reach the port is then
signed in. There is no rate limiting, no audit log, and no multi-tenancy
beyond an owner column.

**If it is false** — if this is put on a network — the honest list of what is
missing is in [pending.md](pending.md) §3.

### 1.4 Python 3.10 or later

`requires-python = ">=3.10"`, and the code uses `X | None` annotations
throughout. Not negotiable without a rewrite of every signature.

---

## 2. About forms

### 2.1 A form is a flat set of named fields, grouped into screens

`FormSchema` is a list of `Field`s with a `screen` and an optional `group`.
There is no repeating section, no nested record, no "add another dependant"
table, no conditional visibility.

**If it is false** — and on real enterprise forms it often is — a repeating
section has to be flattened into `dependant_1_name`, `dependant_2_name` before
anything here can see it, and the model will treat the two as unrelated
columns.

### 2.2 `Field.name` is stable and unique within a form

It is the join key across schema, dataset, model and run. Where the control has
a `name` it is used; otherwise a slug is derived from the id or label.

**If it is false** — a form that renames its controls between versions — a
model fitted on the old names silently predicts nothing for the renamed fields,
because a name it has never seen is a name it has no links for.

### 2.3 A field's meaning can be read off the markup or stated in a spec

The six signals in `fillerai/infer.py` and their confidences (architecture.md
§3) are the whole of it. An `autocomplete` token is trusted at 0.97 because
somebody authored it deliberately; a placeholder is trusted at 0.55 because it
is decoration that sometimes leaks meaning.

**If it is false** — an opaque form where every control is `field_0417` with a
generic label — inference returns `unknown`, generation falls back to free
text, and the run is worth very little. The field spec route exists for
exactly this, and `inspect --evidence` exists to catch it before three more
stages are built on it.

### 2.4 A field's values are either a closed set or essentially unique

`features.py` classifies every column as *enumerable* or *open*, on two tests:
an absolute ceiling (`MAX_DISTINCT = 500`) and a share-of-rows test
(`MIN_ROWS_FOR_SHARE = 32`), so twenty distinct values across twenty records
is recognised as "all different" rather than "a twenty-way choice". Below
`MIN_ROWS = 3` filled rows a field is left alone rather than guessed at.

**This is a real limit, not a formality.** A city column with two thousand
cities is classed open and becomes invisible to the conditional tables even
when it fixes the state exactly — so *more* history makes *fewer* fields
usable, which is backwards. The `linear` engine exists specifically to close
that hole by hashing the value rather than enumerating it, and
[training-and-scale.md](training-and-scale.md) §4 measures where the limit
bites.

### 2.5 An unpredictable field should be declined, not guessed

A claim number is different in every record. `USUAL_FLOOR = 0.25` and
`ACCEPT_ABOVE = 0.7` encode the judgement that a wrong value which *looks*
filled in costs more than an empty box — which is why `seconds_to_notice`
exists in the effort model and is charged against the saving.

This makes coverage a number that must be reported beside accuracy, and never
instead of it.

### 2.6 The relationships in a form are stable over the period being modelled

The model is counts over past records. A plan tier that stops fixing the
deductible in April makes every rule learned before April wrong, and nothing
here notices. There is no drift detection and no retraining trigger.

---

## 3. About generated data

### 3.1 A record is one coherent persona

The unit of generation is an imaginary entity, not a field, and coherence falls
out of that: the city sits in its own state, the area code matches, the email
is built from the name. Fields marked with the same `group` share one address;
different groups are independent.

**What it does not model:** a record that is *two* people (a claimant and an
insured with a real relationship between them) beyond what groups express, and
any relationship between one record and the next — no repeat customers, no
broker who submits forty similar claims, no seasonality.

### 3.2 Generated identifiers must not be able to belong to a real person

By default: SSA has never issued an area number above 899;
`555-0100`–`555-0199` is reserved for fiction; email domains are the RFC 2606
documentation domains; card numbers are Luhn-valid but from published test IIN
ranges. `--realistic-identifiers` turns this off, and output produced that way
should not leave a controlled environment.

### 3.3 The only relationships in generated data are the ones the generator put there

**This is the most important assumption in the project, and it is the one that
limits every result.** An invented persona carries the relationships the
persona code knows about — geography, names, contact details — plus whatever
`follows`/`when` rules the spec declared. Nothing else. A decision tree that
can represent "family policy *and* dependent claimant therefore relationship is
child" has nothing to represent when no such rule was ever written down.

The measured consequence, on five example forms, is in
[process.md](process.md) §8: forms with no declared rules save 3–7%, forms
with 27 and 31 declared rules save 23% and 32%. The engines land within a few
points of each other on all of them, because the ceiling is the information in
the data, not the quality of the model.

**Two things follow, and both are in [pending.md](pending.md) §1:** declaring
rules is worth more than any modelling change, and training on real past
submissions is the thing that would actually move this — needing no change to
the pipeline, because a dataset is a dataset.

### 3.4 A declared rule is true

`follows`/`when` is taken as given by the generator. Nothing checks a
hand-written rule against reality, because there is no reality here to check it
against. A wrong rule produces confidently wrong data, and a model that learns
it perfectly.

(A rule *proposed by a language model* is treated completely differently — see
§6.2.)

---

## 4. About the saving — `fillerai/simulate/effort.py`

**Every number this project reports about time saved rests on five constants,
and none of them has been measured with a stopwatch.** They are assumptions,
they are in one frozen dataclass, and every report prints them underneath
itself rather than quoting a bare percentage.

| Constant | Value | The assumption |
|---|---|---|
| `chars_per_second` | 5.0 | sustained rate on form data — identifiers, codes, postal codes — well below anyone's prose speed, because none of it is muscle memory |
| `seconds_per_field` | 1.2 | finding the next box and focusing it, before a character is typed |
| `seconds_to_pick` | 2.0 | opening a picker and choosing from it |
| `seconds_to_review` | 0.6 | reading a value that is already filled and accepting it |
| `seconds_to_notice` | 1.0 | noticing a filled value is wrong, on top of then retyping it |

Three structural assumptions sit alongside them:

- **A picked control costs the same whatever the value.** Choosing
  "Massachusetts" is the same work as choosing "Ohio", so its cost has nothing
  to do with length (`PICKED_CONTROLS`).
- **Autofill is not free.** The agent is still responsible for what they
  submit, so every filled value is charged a review, and every wrong one is
  charged a notice plus a full retype.
- **On a dense form, most of the time is not typing.** Forty boxes at 1.2s of
  navigation each is the bulk of it, which is why the per-field overhead sits
  beside the typing rate rather than being folded into it.

**What would replace all of this:** a stopwatch on ten real forms. Until then,
the honest reading of "32% less work" is "32% under these five constants", and
the constants are adjustable (`Effort` takes them as arguments) precisely so
somebody with better numbers can substitute them.

Keystrokes, by contrast, are exact: a claim number is fourteen characters
whoever types it. That is why both are reported.

---

## 5. About the model

### 5.1 Confidence must be measured, not computed

A quarter of the records are held back (`holdout=0.25`) and the confidence
curve is fitted on them, binned ten ways with a prior (`CALIBRATION_PRIOR =
5.0`) so three lucky rows cannot certify a band as perfect, and only where
there are at least `MIN_CALIBRATION_ROWS = 20`.

This is what makes a confidence of 0.8 mean the same thing across all six
engines, and it is what keeps naive Bayes usable at all — its raw 0.999 lands
in the top bin and comes out at whatever that bin is really worth.

**Nothing fitted is measured on the rows it was fitted on**, including the
vote-weight combiner, which takes its rows off the *front* of the holdout so
the calibration never describes rows the weights were chosen for.

### 5.2 The evidence is an arbitrary subset of the form

A textbook classifier is trained on a fixed feature set and asked about all of
it. Here the features are whichever boxes the agent happened to type first, the
target is every other box, and the two swap places from one record to the next.

Every engine has to answer from an arbitrary subset, including almost none of
it on the first keystroke. That is why a missing feature descends every branch
of a tree at once (Quinlan's fractional-instance rule) rather than stopping the
walk, and why `linear` presents each record several times during fitting with a
random handful of its fields visible.

It also means one weight set has to serve every evidence size, which is a known
weakness stated plainly in `train/algos/linear.py`.

### 5.3 Association must beat both memorising and luck

Plain accuracy would call every field a brilliant predictor of a country box
that says "US" 94% of the time. So the score is Goodman and Kruskal's lambda —
the share of guessing errors that knowing the predictor removes — with two
corrections, and both are needed: **leave one out**, so a bucket of one is
worth nothing, and **beat a shuffle**, so whatever bucket structure alone
scores is subtracted before any of it counts (`CHANCE_TRIALS = 3`).

Thresholds: `MIN_LAMBDA = 0.1`, `MAX_PREDICTORS = 6`, `MIN_SUPPORT = 20`.

### 5.4 A rule is proposed from meaning and confirmed by data

`derive.py` proposes from the semantic types — a full name is its parts, an age
is arithmetic on a date of birth — and keeps a rule **only if it held on the
training records**. The semantic type says where to look; the data says whether
it is true.

### 5.5 One model averages every agent using it

Two people filling the same queue develop different habits, and the model
mixes them. Per-agent models are a matter of keeping the datasets apart rather
than a change to anything, but nothing does it today.

---

## 6. About the optional LLM features

### 6.1 A language model is worth paying for at build time, not at fill time

The analysis is [llm-modelling.md](llm-modelling.md). The verdict: yes for
proposing `follows`/`when` rules and for semantic typing, which happen once per
form design and offline; no for prediction, because only 6.8% of the form is
headroom a smarter model could win and 89% of what is left blank is closed to
any model.

### 6.2 Anything a model proposes is a guess until it survives the gate

Nothing proposed is written anywhere. `propose` returns proposals, a person
reads them, `apply` is a separate command that never overwrites a
hand-written rule. Before a person even sees one it must pass this project's
own spec parser, a structural check, a cycle check, and 200 generated records
through `validate` and `coherence_report` compared against the same records
generated without it.

### 6.3 A key belongs in the environment, not on disk

Read from the environment; never written to the library, the database, a
config file or the log; `Settings.redacted` shows four characters and is the
only way anything prints it. A key typed into the UI lives in the server
process and is forgotten on restart, and the UI says so where the box is.

### 6.4 Token counts can be approximated, and thinking shares the budget

Counting tokens exactly means a network call, which is the thing being
estimated, so `cost.py` uses four characters to a token and labels the result
an approximation. Separately, a model's own thinking comes out of the same
output budget on both services, which is why `rules_budget()` adds a
`THINKING_ALLOWANCE` of 6,000 on top of `TOKENS_PER_RULE = 90` per field
asked about — and why a truncated answer is retried on a halved batch rather
than assumed impossible.

---

## 7. About operations

### 7.1 Two kinds of credential, with different threat models

A **password** is a short string a person chose, so the hashing cost is the
defence: scrypt at interactive parameters, ~45ms. A **token secret** is 32
bytes from `secrets`, beyond guessing, and is verified on every API call, where
45ms would make the surface useless — so SHA-256. This difference is
deliberate and documented in `fillerai/tokens.py`; a reviewer reading it as an
oversight is the failure mode it is written against.

### 7.2 A session is server-side state

The cookie carries a random id and a signature. The signature is not what makes
it safe — the id is looked up server-side either way — it is what lets a forged
or stale cookie be rejected without a query. Signing out deletes the row, so it
takes effect everywhere at once.

### 7.3 `/v1` must never honour the cookie

A surface that honoured it could be driven by any page open in the user's
browser, which is the entire reason for a second credential.
`Handler._rest` installs a fresh empty `Context` before dispatch.

### 7.4 CORS follows from whether there is a credential

With accounts on, `*` is the default and is safe, because a bearer token is
required either way and `Allow-Credentials` is never sent. With `--no-auth`,
**no** origin is allowed until `--cors-origin` names one, because `*` with no
credential opens the library to any page the user has open.

### 7.5 A library entry belongs to somebody, and is immutable

An entry is keyed by `(id, owner)` together, so two people importing the same
directory get the same ids — the ids carry their lineage — and neither can
reach the other's copy. Entries do not change after they are written, which is
what lets `/v1` cache a loaded model without it going stale.

### 7.6 The database is one file, and Postgres is a subclass

Backend-specific surface is exactly two things: the parameter style and
connecting. The DDL is in the SQL both accept, times are ISO 8601 strings
because the two disagree about timezones. **The driver does not exist** and
will not until a dependency is allowed.

### 7.7 A shipped migration is never edited

Steps are applied in order, once, and recorded, so an older database catches up
on start rather than being recreated.

---

## 8. About the test suite

### 8.1 The suite is the only signal

There is no CI in this repository. 772 tests run offline in about 70 seconds,
and running them before pushing is the whole of the quality gate. See
[pending.md](pending.md) §2.2.

### 8.2 A non-deterministic feature is tested against a recording

Every LLM test replays a fixture through `RecordedTransport`, matched on a
digest of the sorted-key request JSON, and **it raises on a request it has
never seen**. The raise matters as much as the replay: a changed prompt fails
loudly here rather than quietly reaching for the network.

**But a hand-authored fixture is not evidence about what a model says.** The
current fixtures are hand-written, say so in a `_note`, and exercise every
branch of the validation gate. What they cannot tell you is whether a real
model proposes good rules. That is what `livetests/` is for, and it has never
been run — [pending.md](pending.md) §1.1.

---

## See also

- [architecture.md](architecture.md) — where each of these lives in the code.
- [process.md](process.md) — the sequence these hold during.
- [pending.md](pending.md) — the ones that are known problems rather than
  chosen constraints.
