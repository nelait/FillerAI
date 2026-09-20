# Training, serving, and what happens at 20,000 records

This document covers three questions that the [README](../README.md) does not
answer directly. The README explains *why* the model works the way it does —
why a weighted vote rather than naive Bayes, how a predictor earns its
weight, what confidence means. This one is the engineering view sitting
underneath that:

1. What the training run actually does, stage by stage, and what it costs.
2. What the model file is, and what it takes to serve it against a real form.
3. Whether any of it survives real training data rather than generated data.

Everything with a number attached was measured, not estimated. The
measurements and how to reproduce them are at the end.

---

## 1. What training produces

Training produces one JSON file and nothing else. There is no binary, no
weights blob, no runtime to install alongside it. That is a deliberate
consequence of the no-dependency rule, and it is what makes the serving
story in section 3 as simple as it is.

The file is written by `AutofillModel.to_dict` in
[`fillerai/train/model.py:374`](../fillerai/train/model.py) and has six parts:

| part | what it is |
|---|---|
| `schema` | the form itself: every field, its meaning, its options, which screen it is on |
| `profiles` | one per writable field — what values it takes, how often, and whether it is answerable at all |
| `engine` | what the chosen algorithm learned: conditional tables, trees, a record sample, or likelihoods |
| `derivations` | arithmetic rules that survived checking (full name from its parts, age from a date of birth) |
| `calibration` | a curve mapping a raw score to how often scores like it actually turned out right |
| `settings`, `trained_on`, `held_out`, `algorithm`, `model_version` | how to reproduce the run |

On the claims form — 44 fields — trained on 300 records with the default
algorithm, that is 135 KB, split roughly: schema 15 KB, profiles 21 KB,
engine 12 KB, calibration 217 bytes.

The important structural fact is that **the model is a summary of the
training data, not a copy of it.** Its size is governed by the shape of the
form and the number of distinct values per field, not by how many records
went in. Section 4 shows what that buys.

The one exception is the `nearest` algorithm, which by design keeps real
records to search — capped at 600 rows
([`fillerai/train/algos/nearest.py:45`](../fillerai/train/algos/nearest.py)).
That cap exists for privacy, not performance.

---

## 2. What a training run does

`train()` in [`fillerai/train/model.py:462`](../fillerai/train/model.py) runs
five stages in order. Each one is visible in the live log in the UI and in
`-v` on the command line.

**Split.** 25% of records are held back
([`TrainOptions.holdout`](../fillerai/train/model.py), line 121). They are not
used for learning at all; they exist so that the confidence numbers can be
measured rather than asserted.

**Profile every writable column**
([`features.profile_field`](../fillerai/train/features.py), line 184). Each
field is classed as `constant`, `enumerable`, or `open`. This classification
decides everything downstream, and section 5 is about the one place it goes
wrong.

- `constant` — one value in every record.
- `enumerable` — a small enough set of values to choose between. The test is
  two-part: at most `MAX_DISTINCT` (500) distinct values, *and* at most half
  the filled rows, so twenty values across twenty records is recognised as
  "all different" rather than "a twenty-way choice".
- `open` — everything else. A claim number, a free-text description. Nothing
  can predict these and the model says so rather than guessing.

**Propose and check rules**
([`derive.learn_derivations`](../fillerai/train/derive.py), line 325). Rules
are proposed from the schema's semantic types — a full name is its parts
joined, an age is arithmetic on a date of birth — and then verified against
every record. A rule that does not hold is dropped. This is the most
expensive stage; see section 4.

**Fit the engine.** One of five algorithms, selected per run. The default,
`statistical`, learns conditional tables via
[`associate.learn_links`](../fillerai/train/associate.py) (line 250), scoring
each candidate predictor against a permutation test so that a field's
*bucket structure alone* is not mistaken for a relationship.

**Calibrate** ([`model._calibrate`](../fillerai/train/model.py), line 572).
The model predicts every field of every held-out record, and the resulting
curve is what turns a raw score into a confidence that means what it says. A
reported 0.9 is a claim that answers like this are right about 90% of the
time, and that claim was measured on records the model never saw.

---

## 3. Serving the model against a real form

### What a prediction is

`model.predict(observed)`
([`fillerai/train/model.py:263`](../fillerai/train/model.py)) takes whatever
the agent has typed so far and returns one answer per remaining field:

```json
{
  "field": "home_state",
  "value": "TX",
  "confidence": 0.9659,
  "score": 0.8214,
  "basis": "learned",
  "because": ["home_city = Austin -> TX in 100% of 12 records"],
  "alternatives": [["FL", 0.039], ["CA", 0.032]]
}
```

`because` is what lets an agent accept or reject a value rather than trusting
it blindly, and `basis` says which of the four answer sources produced it
(`rule`, `learned`, `usual`, or `none`).

Loading the model is `AutofillModel.from_json`
([`fillerai/train/model.py:432`](../fillerai/train/model.py)). Standard
library only. A 248 KB model loads in 3 ms and answers in 0.5 ms.

### What exists today

There is an HTTP endpoint, `/api/predict`
([`fillerai/web/server.py:714`](../fillerai/web/server.py)), but it is built
for the UI on the same machine and cannot currently serve another system:

- It takes a `model_id` that is a token into an in-memory dictionary
  ([`fillerai/web/server.py:416-436`](../fillerai/web/server.py)). It is
  evicted when the cache fills and gone when the server restarts. There is no
  way over HTTP to say "serve the model saved in the library as `mdl-xyz`".
- Authentication is browser-shaped: a session cookie plus a CSRF header
  ([`fillerai/web/server.py:100-107`](../fillerai/web/server.py)). There is no
  API key or service token.
- No CORS headers, and responses set `X-Frame-Options: DENY`
  ([`fillerai/web/server.py:1287`](../fillerai/web/server.py)), so a real
  form's page cannot call it from a browser.
- It binds `127.0.0.1` by default
  ([`fillerai/web/server.py:1570`](../fillerai/web/server.py)).

### Three ways to close that, cheapest first

**Import it as a library.** If the form's backend is Python, `from_json` the
model at startup and call `predict`. No network, no serving layer, 3 ms of
startup cost. The smallest possible change and probably right for a first
pilot.

**A real endpoint.** A stateless `POST /v1/predict` taking a model id plus the
typed fields, loading from the library rather than from a cache token. The
prediction core is already stateless and pure, so the work is serving
plumbing: API-key auth, CORS, loading by id, concurrency.

**A browser extension**, for a vendor app nobody can change. The join key
already exists: a field's `name` in the schema is the form control's `name`
attribute ([`fillerai/extract/html_form.py:241`](../fillerai/extract/html_form.py)),
so `home_city` maps to `input[name="home_city"]`. The schema stores no CSS
selector, so controls that only carry an `id` would need the extension's own
matching rules. This also needs the endpoint above first.

### One caution before any of these

A model file contains **real values from the training data** — actual names,
cities and codes with their counts
([`fillerai/train/features.py:217`](../fillerai/train/features.py)). That is
fine on a server. It means a model must never be shipped into the browser as
a static file, and it changes what "downloading a model" means once training
runs on real submissions rather than generated ones.

---

## 4. What happens at 20,000 records

All figures below: the claims intake form (44 fields), 20,000 generated
records, 15,000 to learn from and 5,000 held back, Python 3.11 on a single
core of an Intel Xeon at 2.8 GHz. Treat them as relative costs; absolute
numbers will differ on other hardware.

### Inference does not grow with the data

This is the headline. Because the model is a summary rather than a copy:

| | 300 records | 20,000 records |
|---|---|---|
| model file | 135 KB | 248 KB |
| load | 2 ms | 3 ms |
| predict (whole form) | 0.6 ms | 0.50 ms |

67 times the data made the model 1.8 times larger and did not slow
prediction down at all. Serving is a non-issue at this size and would remain
one at 100,000.

### Training is linear

Default algorithm, measured across five dataset sizes:

| records | training time | per record |
|---|---|---|
| 1,000 | 2.7 s | 2.75 ms |
| 2,500 | 5.9 s | 2.38 ms |
| 5,000 | 11.7 s | 2.34 ms |
| 10,000 | 24.5 s | 2.45 ms |
| 20,000 | 49.6 s | 2.48 ms |

Peak memory at 20,000 records is 132 MB, and essentially all of it is the
records themselves — training adds nothing measurable on top of holding the
dataset.

### Cost per algorithm at 20,000 records

| algorithm | train | model file | load | predict |
|---|---|---|---|---|
| `statistical` | 51 s | 248 KB | 3 ms | 0.50 ms |
| `bayes` | 64 s | 6.3 MB | 82 ms | 1.69 ms |
| `tree` | 69 s | 1.2 MB | 19 ms | 1.50 ms |
| `nearest` | 82 s | 355 KB | 4 ms | 4.35 ms |
| `forest` | **10.4 min** | **23 MB** | 639 ms | 19.8 ms |

Four of the five are a coffee break at worst. `forest` is the outlier in
every column and needs its per-tree record sample capped before anyone runs
it against real data.

Training in the UI is already backgrounded with a live log
([`api_train_start`](../fillerai/web/server.py), line 643), so a 51 s run is
watchable. A ten-minute one is not.

### Where the time goes

Profiling the default 20,000-record run (shares, not absolute times — the
profiler adds its own overhead):

| stage | share |
|---|---|
| rule verification ([`derive._verify`](../fillerai/train/derive.py), line 285) | 56% |
| calibration ([`model._calibrate`](../fillerai/train/model.py), line 572) | 23% |
| association scoring ([`associate.learn_links`](../fillerai/train/associate.py), line 250) | 17% |

Rule verification applies each candidate rule to all 15,000 records — 24
million calls. Calibration predicts every field of every held-out record.
Association scoring runs a permutation test per field pair.

All three would give the same answers on a sample. A rule that holds on 2,000
records holds on 15,000; a calibration curve fitted on 2,000 held-out records
is the same curve. So if training time ever becomes a problem, it comes down
a long way without touching any of the mathematics. It is not a problem at
20,000 records today, which is why nothing has been changed yet.

---

## 5. What to fix before real data

### The distinct-value ceiling

This is the one that matters, and it is the reason this document exists.

A field with more than `MAX_DISTINCT` (500) distinct values is classed `open`
([`fillerai/train/features.py:29`](../fillerai/train/features.py)). And
`learn_links` admits only `enumerable` and `constant` fields
([`fillerai/train/associate.py:260-263`](../fillerai/train/associate.py)) —
as the field being predicted **and as the field doing the predicting**. So an
`open` field is invisible to the engine in both directions.

Demonstrated on a two-column dataset where city determines state perfectly,
20,000 records each time, varying only how many distinct cities appear:

| distinct cities | `city` classed as | `state` predicted |
|---|---|---|
| 100 | enumerable | correct, confidence 1.00 |
| 400 | enumerable | correct, confidence 1.00 |
| 600 | **open** | **nothing at all** |
| 2,000 | **open** | **nothing at all** |

The relationship is identical in all four cases. Only the cardinality
changed. **More history makes fewer fields eligible**, which is backwards
from how the system should behave.

This never shows up on generated data, because the generator draws from fixed
catalogs — the claims form's cities come from a pool of 78, so a 20,000-record
synthetic run looks identical to a 300-record one. Real history is exactly
where high-cardinality fields live: city, provider, employer, diagnosis code,
policy type. This will be hit immediately.

Raising the constant is not the fix, because the cost of a wide conditional
table is real. The fix is to keep the frequent values as buckets and put the
long tail somewhere — a frequency floor with a tail bucket, prefix or hash
bucketing for codes — so that a high-cardinality field stays usable as a
predictor even when it cannot be enumerated. It is contained to how a field
is profiled and how a link table is built.

### The UI record cap

`MAX_RECORDS = 5000` ([`fillerai/web/server.py:64`](../fillerai/web/server.py),
enforced at line 443) means the web UI refuses a 20,000-record dataset
outright. The CLI has no such cap. The number was chosen when the UI trained
synchronously; it needs to rise, together with a decision about what the
sensible ceiling actually is now that training is backgrounded.

### Forest at scale

Ten minutes and a 23 MB model at 20,000 records, versus 69 seconds and 1.2 MB
for a single tree. The per-tree record sample needs a cap in the same spirit
as `nearest`'s 600-row cap.

---

## 6. Reproducing the measurements

From a clone, with no setup:

```bash
# 20,000 records against the claims form.
python -m fillerai generate examples/out/claims_intake.schema.json \
    -n 20000 --seed 11 -o big.json

# The default algorithm, with the live log.
time python -m fillerai train examples/out/claims_intake.schema.json \
    big.json -o big-stat.json -v

# Any other algorithm.
time python -m fillerai train examples/out/claims_intake.schema.json \
    big.json -a forest -o big-forest.json

# Load and prediction cost.
python -c "
import time
from fillerai.train.model import AutofillModel
t = time.time(); m = AutofillModel.from_json(open('big-stat.json').read())
print(f'load {(time.time() - t) * 1000:.0f} ms')
t = time.time()
for _ in range(100):
    p = m.predict({'first_name': 'Dana', 'home_city': 'Austin'})
print(f'predict {(time.time() - t) * 10:.2f} ms')
"
```

The cardinality experiment in section 5:

```bash
python -c "
import random
from fillerai.schema import Field, FormSchema
from fillerai.train import model as M

STATES = ['TX', 'CA', 'NY', 'FL', 'IL']
schema = FormSchema(name='t', fields=[
    Field(name='city', semantic_type='city'),
    Field(name='state', semantic_type='state'),
])

def records(n_cities):
    rng = random.Random(3)
    cities = [(f'City{i}', STATES[i % 5]) for i in range(n_cities)]
    return [dict(zip(('city', 'state'), cities[rng.randrange(n_cities)]))
            for _ in range(20000)]

for n in (100, 400, 600, 2000):
    m = M.train(schema, records(n))
    p = m.predict({'city': 'City7'})['state']
    print(f'{n:>5} cities: city is {m.profiles[\"city\"].kind}, '
          f'state = {p.value!r} at {p.confidence:.2f}')
"
```

---

## Summary

The architecture holds at 20,000 records and would hold well beyond it.
Inference is independent of training-set size, training is linear with a
reasonable constant, and memory is dominated by the dataset rather than by
anything the training does.

Three things need attention before real data, in this order:

1. **The distinct-value ceiling**, which silently drops exactly the
   high-cardinality fields that real history is full of.
2. **The UI's 5,000-record cap**, which is now just a stale number.
3. **Forest's cost at scale**, which needs the same kind of sampling cap the
   nearest-records algorithm already has.

None of these is architectural. All three are contained changes to code that
already exists.
