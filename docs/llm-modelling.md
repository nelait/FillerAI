# LLM-based modelling: where it would earn its place

FillerAI reads a form, invents records for it, learns to finish it, and costs
the saving. A language model could sit at any of those four stages. This
document works out which ones it belongs in — what training and inference
would look like in each, what it would cost per form and per year, and how it
compares against what the six engines already do.

**This is an analysis, not a change.** Nothing in `fillerai/` was touched to
write it. The conclusion, up front, is that an LLM earns its cost at *build*
time and does not earn it at *fill* time, and that the reason is more
interesting than the bill: on this pipeline's own data there is almost no
headroom left for a better predictor to win.

Everything with a number attached was measured on `main` against
`examples/auto_insurance_quote.fields.json`, 800 generated records and 150
held-out forms. The scripts are in [section 9](#9-the-scripts). Costs sit
beside [**Training, serving, and what happens at 20,000
records**](training-and-scale.md) rather than replacing them, and the argument
in [section 2](#2-cold-start-and-only-cold-start) leans on the finding in
[**Weight-based training**](weight-based-training.md) that invented records
only carry the relationships the generator put in them.

---

## 0. The six places, and the verdict on each

| Stage | Verdict | Why |
| --- | --- | --- |
| Authoring the generation rules | **Yes — first** | One call per form, ~$0.06, no records in the input. The `follows`/`when` rules are what moved the numbers in 0.7.0, and writing them is hand work. |
| Semantic typing at extraction | **Yes** | The regex ladder gets five fields on one form confidently wrong, above the threshold that `inspect` flags. |
| Free text and high-cardinality targets | **A real gap** | An `open` field is never a prediction target for any engine. Nothing here can draft an incident description. |
| Autofill prediction at fill time | **No, on this evidence** | 89% of what is left unfilled is unknowable by anything. See section 1. |
| Confidence | **No — but testable** | A stated confidence is asserted; this project's is measured. The useful move is to put an LLM behind the same isotonic curve. |
| Generating the records | **No, emphatically** | 20,000 records take 7.6 seconds and cost nothing. The same rows through a model are $37–185, hours of wall clock, and not reproducible from a seed. |

---

## 1. What is actually left to win

Any argument for a smarter predictor has to begin with the gap it would close,
and the gap is smaller than it looks. The setup: 800 records of
`auto_insurance_quote` (54 fields), the `statistical` engine, 150 held-out
forms, three seed fields typed. The headline is the familiar one —

```
over 150 forms, typing 3 field(s) each: 19 of 51 fields filled at 95%
accuracy, 2m 14s a form against 3m 00s by hand (25% less)
```

(Slightly ahead of the README's 16.8 of 51.1 for this form, which is fitted on
fewer records. Every figure in this document comes from the one run described
above, so they are consistent with each other rather than with that table.)

— which leaves 7,664 cells the model had an opinion about. Sorted by what the
model said and why:

| | cells | share |
| --- | ---: | ---: |
| **Filled** at ≥0.70, 95% of them right | 2,791 | 36% |
| Left blank: *different in every record* | 1,200 | 16% |
| Left blank: *no usual value at all* | 600 | 8% |
| Left blank: *no field seen so far narrows this* | 2,554 | 33% |
| Left blank: a real guess, on thin support | 519 | 7% |

The two middle rows are twelve fields — first name, last name, date of birth,
email, phone, street, VIN, odometer, years licensed, vehicle year, prior
carrier, effective date. A language model does not know this applicant's VIN.
Neither does anything else, and no amount of training data changes that; the
model already says so, and says what shape the value takes, which is the
useful answer.

The fourth row is the one worth dwelling on, because it is the largest and it
is the one an LLM is supposed to fix. Those 2,554 cells are marital status,
licence status, SR-22 required, vehicle use, annual mileage band, payment
plan, instalments, payment frequency, autopay, paperless billing,
good-student discount, defensive-driving course. Every one is a free choice
the applicant makes. From a ZIP code, a car model and a deductible, no amount
of reasoning recovers whether someone is married or wants paper bills. The
engine's own wording is exactly right:

```
marital_status   no field seen so far narrows this; Single is the usual
                 value, in 27% of records
```

So of the 4,873 cells left blank, **4,354 — 89% — are closed to any model at
all.** Only the last row, 519 cells where the engine had a genuine guess
resting on two or three supporting records, is territory a better predictor
could take. That is **6.8% of the form**.

This is the central finding and it is not about language models: *the ceiling
on this task is information, not intelligence.* An engine that answered every
answerable field perfectly would fill about a fifth more cells than this one
does — call it 25% of the work saved rising to somewhere near 30% — and then
stop, because the rest of the form is not derivable from what the agent has
typed.

---

## 2. Cold start, and only cold start

There is one place a language model genuinely knows something the engines do
not, and it shows up clearly when the 22 fields the engine *does* fill are
sorted by where their answer comes from.

**Facts about the world** — nine fields, all following from the ZIP code and
the vehicle model:

```
garaging_city  garaging_state  license_state  vehicle_make
body_style     fuel_type       vehicle_class  doors          seats
```

A Wrangler is a Jeep. 78205 is San Antonio, Texas. A model knows these with no
training data whatsoever; the engine has to earn them from co-occurrence
counts, one ZIP code at a time.

**This carrier's own product rules** — ten fields, all following from the
collision deductible:

```
coverage_tier  comprehensive_deductible  bodily_injury_limit
property_damage_limit  uninsured_motorist  roadside_assistance
rental_reimbursement   glass_coverage      gap_coverage
accident_forgiveness
```

No model knows these, because they are not facts about the world — they are
one insurer's product ladder. An LLM would have to be told them or shown
enough examples to infer them, at which point the prompt *is* the model, and
you are paying per form for what a 248 KB lookup table does in 0.6 ms.

So the LLM's advantage is precisely nine fields, and it holds them from record
zero. How long does the engine take to catch up? Same seeds, same 150
held-out forms, varying only how many records were fitted:

| records fitted | fields filled | accuracy | work saved |
| ---: | ---: | ---: | ---: |
| 25 | 10.0 | **68%** | 9.0% |
| 50 | 12.7 | 89% | 16.0% |
| 100 | 12.7 | 94% | 17.0% |
| 200 | 14.4 | 95% | 19.5% |
| 400 | 16.6 | 96% | 22.6% |
| 800 | 18.6 | 95% | 25.1% |

The 25-record row is the interesting one and it is the only unambiguous case
for an LLM in this whole document: the engine fills ten fields and gets a
third of them **wrong**, which by the project's own effort model is worse than
filling nothing, since a wrong autofill costs `seconds_to_notice` on top of
typing the value out. A model would answer the nine world-knowledge fields
correctly there.

And then the advantage evaporates. By 50 records the engine is past nine
fields, by 200 it is comfortably ahead, and by 800 it fills twice what world
knowledge alone can reach — because half of what it fills is the product
ladder an LLM cannot know.

The catch is what it costs to leave the cold-start region:

```
20,000 records in 7.6s = 2,616 records/s, 0.38 ms/record
```

Getting from 25 records to 800 is **0.3 seconds of CPU**. That is the whole
cold-start advantage, priced.

---

## 3. What it would cost

A per-form autofill call divides cleanly into a cacheable prefix and a tiny
variable part, which is the best case for prompt caching:

| part | tokens | changes per form? |
| --- | ---: | --- |
| The schema, rendered compactly (name, label, semantic type, control, options) | ~2,300 | no |
| Instructions and output format | ~500 | no |
| 20 example records as few-shot | ~7,350 | no |
| The fields the agent has typed | ~60 | **yes** |
| Output: 51 values with confidences, as structured JSON | ~1,000 | **yes** |

The compact schema measures 9,272 characters for this 54-field form; a single
record is 1,484. Token counts are estimated at four characters per token and
are approximate.

A cache read costs a tenth of the input price, so the 10,150-token prefix is
nearly free to re-send. Output dominates:

| model | input / output per MTok | per form, cached | 1.25M forms a year |
| --- | ---: | ---: | ---: |
| Haiku 4.5 | $1 / $5 | $0.0061 | $7,600 |
| Sonnet 5 | $2 / $10 | $0.0122 | $15,250 |
| Opus 5 | $5 / $25 | $0.0304 | $38,000 |
| the six engines today | — | $0 | $0 |

Without caching these roughly triple, since the prefix would be charged at
full input rate every form. The first call in each cache window pays a 1.25×
write instead, which amortises to nothing at any real volume. For the offline
uses in section 7 — rule authoring, batch semantic typing — the Batch API
halves everything again.

Now the other side of the ledger. The simulation says 45 seconds saved per
form; 1.25M forms is about 15,600 hours of agent time a year. Even Opus 5
works out at roughly **$2.40 per hour of agent time saved**.

Which is the point worth making plainly: **cost is not the argument against a
fill-time LLM.** If it added coverage it would be trivially worth buying. The
argument is section 1 — on this data there is barely any coverage left to add
— followed by sections 4 and 5.

---

## 4. Latency, in this project's own units

`fillerai/simulate/effort.py` is unusually useful here, because it has already
committed to what a second is worth:

```python
chars_per_second: float = 5.0     # typing form data, not prose
seconds_per_field: float = 1.2    # finding the next box and focusing it
seconds_to_pick: float = 2.0      # opening a picker and choosing
seconds_to_review: float = 0.6    # reading a filled value and accepting it
seconds_to_notice: float = 1.0    # spotting a wrong one, before retyping
```

Against that clock:

| | whole form |
| --- | ---: |
| `statistical`, at 800 records | **0.6 ms** |
| `forest`, at 20,000 records | 19.8 ms |
| An LLM, ~1,000 output tokens | 10–20 s |
| The same in fast mode (up to 2.5× output rate) | 4–8 s |
| The entire saving the model produces | 45 s |

A blocking call eats a quarter to a half of the saving it exists to create.
There is exactly one design that survives this: fire the call **once**,
asynchronously, the moment the seed fields are typed; let the agent carry on
down the form; stream values in behind them. That is buildable, and the
existing `api_train_start` background-worker pattern in `fillerai/web/server.py`
is the right shape for it — bearing in mind the ownership bug that pattern
already produced once, where a worker thread lost the request's user.

But note what the design rules out. Reacting to every new field the agent
types — the thing a language model is supposedly good at — means another ten
seconds per re-ask. The engines re-predict the whole form in 0.6 ms, which is
why the UI can afford to do it on every keystroke.

---

## 5. Privacy, which is the reason this project exists

The README's premise is that companies will not hand over their form data, and
that is why synthetic generation is stage one rather than an afterthought. It
is worth being blunt about what a fill-time LLM does to that premise.

**The fields an agent has typed are not synthetic.** They are a live
claimant's name, date of birth, SSN, diagnosis code. Sending those to an
external API is a strictly larger ask than sharing the historical data would
have been, and a security team that refused the second will certainly refuse
the first. Every guarantee in *"Identifiers cannot belong to a real person"* —
the 900-999 SSA area numbers, the 555-0100 phone block, the RFC 2606 domains —
applies to generated data and to nothing that happens at fill time.

Four ways out, honestly ranked:

1. **Never send live data.** Use the model at build time only — extraction and
   rule authoring, where the input is field names and markup. This is what
   section 7 recommends, and it is the only option that needs no security
   conversation at all.
2. **Bedrock, Vertex or Foundry inside the customer's own cloud account and
   region.** The standard enterprise answer and usually the one that actually
   gets approved, because the data never leaves their boundary. It costs
   first-party feature parity: prompt caching and fast mode behave differently
   there, which matters given section 3 depends on caching.
3. **The first-party API under zero data retention**, with `inference_geo`
   pinning where inference runs. Workable, still a data-leaves-the-building
   conversation.
4. **A local model.** Solves privacy completely and breaks the stdlib rule far
   harder than an HTTP call does — now there are weights and a runtime to ship
   into the locked-down environment.

---

## 6. The stdlib rule, and which half an LLM breaks

The README states two things in one breath: *"Nothing here talks to a network,
and there are no dependencies."* They are separate constraints, and it matters
which one is being defended.

**The dependency rule survives, technically.** The Messages API is HTTPS and
JSON, so `urllib.request` plus `json` calls it in about thirty lines with
nothing installed. What that gives up is what the SDK does: typed errors,
retry with backoff, streaming, prompt-cache headers, token counting. A
hand-rolled client is fine for a spike and wrong for anything that ships; if
an LLM feature is ever more than an experiment, take the dependency rather
than reimplement retries badly.

**The network rule is the load-bearing one**, and it should not bend. It is
what lets FillerAI run inside the environment where the real form lives, which
is the whole reason the project is built the way it is. So the shape of any
LLM feature is fixed before its content:

- an optional module the core never imports,
- off by default, with no API key needed to run anything that exists today,
- and every stage — `extract`, `generate`, `train`, `simulate` — still working
  end to end with it off.

A pipeline whose `extract` now needs an API key is a different product, and a
worse one.

---

## 7. Where an LLM does belong

### 7.1 Proposing the generation rules

This is the highest-leverage use in the project by a distance, and it follows
directly from what 0.7.0 found.

Before 0.7.0 every `<select>` on every form was filled with
`rng.choice(field.options)`. So business fields were uniform noise, the only
structure a model could find was what a persona brings with it, and autofill
only ever recovered addresses. The fix was declarative — a field spec carries
the form's own rules via `follows`, `when` and `otherwise` — and it moved the
numbers further than any modelling change in this project's history:

```
claims_intake           4.2/41.6 boxes    5.0%
auto_insurance_quote   16.8/51.1 boxes   23.3%
employee_onboarding    29.0/50.1 boxes   34.5%
```

But somebody has to write those rules. `auto_insurance_quote` declares 27 of
them and `employee_onboarding` 31, by hand, per form.

That is precisely a language job, and it is the best-shaped one here:

- **The input contains no records.** A schema is field names, labels, semantic
  types and option lists. There is no privacy question to have, which is what
  makes this the first thing to build rather than the third.
- **The output goes through machinery that already exists.** The rules parse
  into `schema.Derived`, `coherence_report` names unknown sources,
  `dataset._resolution_order` catches cycles, `--check` verifies the rows, and
  `_as_written` deliberately refuses to fall back to a random option — so a
  wrong proposed rule is *reported*, not silently turned back into the noise
  the feature removed.
- **Nothing downstream changes.** The model writes rules; the generator still
  renders the rows. Determinism, seed reproducibility, identifier safety and
  the zero marginal cost of a record are all untouched.
- **It costs almost nothing.** ~2,300 tokens in, ~2,000 out: about **$0.06 on
  Opus 5**, once, for a form design that then generates forever.

A human reviews the proposed rules before they go in the spec, which is easy,
because a rule is one line and says what it means.

One caveat to write into the feature, since the README already says it about
declared rules generally: a model proposing rules is *inventing plausible
business logic*, not observing a real one. It gets `vehicle_model → vehicle_make`
right because that is a fact; it will guess at a carrier's coverage ladder.
The generated data is only as honest as the rules, so review is not optional.

### 7.2 Semantic typing as a second opinion

The weighted-evidence ladder in `fillerai/infer.py` is good, and it is wrong
in one specific way that a language model is not: it matches regexes against a
field's *name* before considering what the field actually is. On
`auto_insurance_quote` alone:

| field | inferred as | confidence | what matched |
| --- | --- | ---: | --- |
| `telematics_eligible` | `phone` | 0.80 | name matched `/tel/` |
| `prior_claims` | `claim_number` | 0.80 | name matched `/claim/` |
| `claim_free_discount` | `claim_number` | 0.80 | name matched `/claim/` |
| `business_use_rider` | `company` | 0.74 | label matched `/business/` |
| `lienholder_required` | `policy_number` | 0.74 | label matched `/policy/` |

Every one of those is a yes/no `<select>`. And every one scores **above 0.70**,
so `inspect` prints no `?` beside it and nobody looks. That is what makes this
worth fixing: the *flagged* headroom is tiny — across the five example forms
only 6 fields of 204 fall below 0.70, and only three come back `unknown`
(`vin`, `prior_carrier`, `desk_number`) — while the silent errors are the ones
that hurt. A wrong semantic type is not cosmetic, because generation is driven
entirely by `semantic_type`: `telematics_eligible` typed as `phone` renders
phone numbers into a Yes/No column.

The right shape is not replacement but **disagreement**. Run a model over the
schema, keep the ladder's answer, and surface only the fields where the two
differ, with both readings and the evidence behind each. Across all five
example forms that is a handful of rows for a person to arbitrate rather than
204. The ladder's `evidence` list already exists to make exactly this kind of
review possible.

The same call can flag the taxonomy's own edges. `time_zone` and `desk_number`
have no member among the 47 semantic types and fall through to `enum` and
`unknown`; 25 of `employee_onboarding`'s 53 fields sit at exactly 0.72 on
*"closed set of options, no other signal"*, which is the ladder saying it has
nothing to offer beyond noticing they are dropdowns.

### 7.3 A seventh engine, if the question needs settling

The architecture makes this unusually cheap to answer properly instead of
arguing about it, and that is worth using.

An engine is one file in `fillerai/train/algos/` implementing `Engine` and
calling `register()`. A `Guess` carries a raw `score` — explicitly documented
as "a probability under that engine's own assumptions, which are wrong in
their own particular way" — and the shared isotonic calibration converts it
into the number a user reads. That is the machinery that keeps naive Bayes
usable despite its raw scores being nonsense, and it would do the same job
here: **an LLM's confidence would become measured rather than asserted**, on
the same held-out records, against the same curve. Then `train --compare`
reports it beside the other six and the question closes on evidence.

Two things to plan for:

- **Calibration is the expensive part, not the fit.** It predicts every field
  of every held-out record at five evidence sizes, which is why `forest`'s ten
  minutes at 20,000 records turned out to be 9.5 minutes of calibration and
  12.7 seconds of fitting. At 5,000 held-out records that is tens of thousands
  of calls. Cap the holdout hard, or route calibration through the Batch API.
- **Expect it to lose on generated data,** for the reason in section 1 and the
  reason in [weight-based-training.md](weight-based-training.md): invented
  records carry only the relationships the generator put in them, so there is
  nothing subtle for a cleverer model to find. A result of "the LLM engine
  scored no better" on synthetic data is the expected result, not a bug, and
  it would say very little about real history.

### 7.4 Free text, which nothing here does at all

This is the one place an LLM adds a *capability* rather than a percentage.

A field with more than 500 distinct values is classed `open`, and an `open`
field is never a prediction target for any engine — `fillerai/train/model.py`
lines 317 and 494. The `linear` engine was built for cardinality, but it
addresses the problem from the *source* side, via hashed buckets; nothing
predicts an open field. How much of a form that is:

| form | fields | `open` | share |
| --- | ---: | ---: | ---: |
| `claims_intake` | 45 | 22 | 49% |
| `member_enrollment` | 31 | 15 | 48% |
| `patient_registration` | 21 | 11 | 52% |
| `employee_onboarding` | 53 | 9 | 17% |
| `auto_insurance_quote` | 54 | 8 | 15% |

Those counts overstate the opportunity, because on generated data most open
fields are identifiers that nothing could predict. But real intake forms carry
"describe the incident" boxes, and a real agent writes those from the
structured fields around them. No engine here can touch that, and a language
model is the only tool in the discussion that can.

Worth scoping separately from autofill, because the confidence story is
different in kind: you cannot say a paragraph is 0.82 right, so the isotonic
curve does not apply and the interaction has to be a draft the agent edits
rather than a value the agent accepts.

### 7.5 Not the rows

For completeness, since it is the first idea most people have. Generating the
dataset with a model instead of the generator:

| | the generator | an LLM |
| --- | --- | --- |
| 20,000 records | **7.6 s** | hours, or a Batch job |
| cost | $0 | ~$37 Haiku, $74 Sonnet 5, $185 Opus 5 |
| same seed, same rows | yes | no |
| SSA-unissued SSNs, 555-0100 phones, Luhn-valid test IINs | guaranteed | hoped for |
| city/state/ZIP drawn from one row of a table | guaranteed | hoped for |
| `--check` coherence pass | passes by construction | must be re-checked |

Rules from a model, rows from the generator.

---

## 8. What would change this conclusion

Every number above is measured on **synthetic data with declared rules**, so
the model is recovering structure we put there ourselves. That is fair for a
demonstration and it is not the same as measuring the thing. Three ways real
submission history changes the answer, all of them in the language model's
favour:

**High-cardinality predictors become the interesting ones.** Employer,
referring provider, diagnosis code, city on a national dataset — fields with
thousands of values that `MAX_DISTINCT = 500` drops today, as a target *and*
as a source. The `linear` engine exists for exactly this and has never been
tested against a column that behaves like a real one. A model that has read
the world already knows that a provider name implies a specialty.

**Habits appear that no generator invents.** Real submissions carry what a
particular office actually does — the orders in which things get filled, the
defaults a team has silently standardised on. That is the kind of structure a
model with world knowledge is well placed to notice and, more usefully, to
*name*, which is a different job from predicting it.

**Free text shows up.** On generated forms the open fields are mostly
identifiers. On a real claims history they include narrative, and section 7.4
becomes the main event rather than a footnote.

None of that argues for a fill-time LLM tomorrow. It argues that the
extraction and rule-authoring work in sections 7.1 and 7.2 is worth doing now,
because it is cheap, offline and touches no customer data — and that the
prediction question is worth re-asking on the day there is one real dataset to
ask it against.

---

## 9. The scripts

Every figure above comes from these. Run from the repository root; nothing
here needs a network or a key.

**Section 1 — the cell breakdown.**

```python
import collections
from fillerai.generate.dataset import generate, Options
from fillerai import extract_spec, train, suggest_seed_fields, simulate

sch = extract_spec('examples/auto_insurance_quote.fields.json')
rows = [dict(r) for r in generate(sch, Options(count=1000, seed=3)).records]
model = train(sch, rows[:800], seed=1, algorithm='statistical')
seeds = suggest_seed_fields(model, 3)
cases = rows[800:950]

bysrc, reasons = collections.Counter(), collections.Counter()
for c in cases:
    run = simulate(model, {k: c.get(k) for k in seeds}, case=c)
    for cell in run.to_dict()['cells']:
        bysrc[cell['source']] += 1
        if cell['source'] in ('yours', 'suggested'):
            b = (cell.get('because') or [''])[0]
            reasons['unique per record' if 'different in every record' in b
                    else 'no usual value' if 'no usual value' in b
                    else 'nothing narrows it' if 'narrows' in b
                    else 'thin support'] += 1
print(dict(bysrc)); print(reasons.most_common(4))
```

**Section 2 — the split of filled fields, and the cold-start curve.**

```python
from fillerai import simulate_many

for f in sch.fields:                      # which fields ever get filled
    n = sum(1 for c in cases
            if any(x['name'] == f.name and x['source'] == 'filled'
                   for x in simulate(model, {k: c.get(k) for k in seeds},
                                     case=c).to_dict()['cells']))
    if n:
        print(f'{f.name:30s} {n:4d}/150  {f.semantic_type}')

for n in (25, 50, 100, 200, 400, 800):    # the curve
    s = simulate_many(train(sch, rows[:n], seed=1), cases, seeds).to_dict()
    print(f"{n:4d} -> filled {s['per_case']['filled']:5.1f}/51  "
          f"acc {s['accuracy']:.3f}  saved {s['share_saved'] * 100:4.1f}%")
```

**Section 2 — generation throughput.**

```python
import time
t = time.time(); generate(sch, Options(count=20000, seed=3))
print(f'{20000 / (time.time() - t):.0f} records/s')
```

**Section 3 — prompt sizes.**

```python
import json
compact = '\n'.join(
    f"{f.name} | {f.label} | {f.semantic_type} | {f.control}"
    + (f" options={'|'.join(map(str, f.options))}" if f.options else '')
    for f in sch.fields)
print('schema', len(compact), 'chars')
print('20 records', len('\n'.join(
    json.dumps(r, separators=(',', ':')) for r in rows[:20])), 'chars')
```

**Section 7.2 — the silently wrong semantic types.**

```python
for f in sch.fields:
    print(f'{f.name:28s} -> {f.semantic_type:16s} {f.confidence:.2f}  '
          f'{(f.evidence or [""])[0][:55]}')
```

**Section 7.4 — how much of each form is `open`.**

```python
import collections, glob, os
from fillerai import extract_html
for p in sorted(glob.glob('examples/*')):
    s = (extract_html(p) if p.endswith('.html')
         else extract_spec(p) if p.endswith('.fields.json') else None)
    if s is None:
        continue
    m = train(s, [dict(r) for r in generate(s, Options(count=600, seed=3)).records], seed=1)
    kinds = collections.Counter(pr.kind for pr in m.profiles.values())
    print(f'{os.path.basename(p):40s} {len(s.fields):3d} fields  {dict(kinds)}')
```

All figures: Python 3.11, single core, `main` at the time of writing. Prices
are published first-party API rates as of September 2026, and token counts are
estimated at four characters per token. Treat all of them as relative costs.
