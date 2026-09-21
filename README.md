# FillerAI — read a form, fill a form

Customer-facing agents fill hundreds of fields across many screens. Training
anything to help with that needs data, and the companies that have the data
usually cannot share it. So FillerAI starts by not needing any: point it at a
form and it produces as many realistic, internally consistent records as you
want, without a single real record ever being involved. Then it learns from
those records how to finish the form from the first few fields an agent
types.

Nothing here talks to a network, and there are no dependencies. Python 3.10
or newer and the standard library is the whole requirement, which is the
point — it has to run inside the locked-down environment where the real form
lives.

```bash
# The UI, if you would rather see it than type it.
python -m fillerai serve --open

# What does this form actually ask for?
python -m fillerai inspect examples/claims_intake.html

# Save the field schema.
python -m fillerai extract examples/claims_intake.html -o schema.json

# 500 records, reproducible, checked before they are written.
python -m fillerai generate schema.json -n 500 --seed 42 --check -f csv -o data.csv

# Learn to finish the form, and say how well it can. Watch it work.
python -m fillerai train schema.json data.csv --seed 1 -o model.json -a forest -v

# Which algorithm suits this form? Stop guessing and measure.
python -m fillerai train schema.json data.csv --seed 1 --compare

# Given a ZIP, what else does it know?
python -m fillerai predict model.json --set home_postal_code=78701

# Put it in front of 50 forms it has never seen. What did it save?
python -m fillerai simulate model.json -n 50 --seed 7

# What did all of that leave behind, and what came from what?
python -m fillerai library list

# Who can sign in to the UI, and who can hand out accounts?
python -m fillerai users add krishna --admin
python -m fillerai db status
```

## Why the records hang together

The unit of generation is not a field, it is a **persona** — one imaginary
person, their addresses, their employer, their account identifiers. Each
field is then rendered by asking that persona for the fact it wants. Sensible
records fall out of that instead of being patched up afterwards:

| Relationship | How it holds |
| --- | --- |
| City, state and ZIP | Drawn from one row of a geography table, never assembled from parts |
| Phone area code | Taken from the address the record actually shows |
| Email | Built from the persona's own first and last name |
| Age and date of birth | Age is derived from the date, so they cannot drift |
| Title and gender | Honorific is picked from the ones that fit the gender |
| Card number, expiry, CVV | All three belong to one card |
| Billing vs shipping | Separate addresses, each internally consistent |
| Referring provider, spouse | A *different* person, not the subject echoed back |

Fields are sorted into **groups** during extraction, from a `data-group`
attribute, a `<fieldset>`, or a shared name prefix such as `billing_`. A group
gets its own address, so `shipping_city` sits in `shipping_state` while
`billing_*` goes somewhere else entirely. Names in `PARTY_PREFIXES`
(`spouse`, `beneficiary`, `referring`, `provider`, …) mean a different person
rather than the same person's second address, and get their own persona.

Given names are deliberately **not** gender-coded. Real populations do not
work that way, and a model trained on data where they do would learn a
correlation that is not there.

## The field schema

`FormSchema` is the contract the later phases build on, so it is versioned
and additive — consumers ignore keys they do not know rather than failing.
One field looks like this:

```json
{
  "name": "home_postal_code",
  "label": "ZIP Code",
  "semantic_type": "postal_code",
  "data_type": "string",
  "control": "text",
  "screen": "addresses",
  "group": "home",
  "constraints": {"required": true, "max_length": 10, "pattern": "\\d{5}(-\\d{4})?"},
  "confidence": 0.98,
  "evidence": ["autocomplete=postal-code", "name matches /(zip|postal|...)/"]
}
```

`semantic_type` is what the field *means* (one of 47: `postal_code`,
`date_of_birth`, `routing_number`, `diagnosis_code`, …), as opposed to
`data_type`, which is only its shape. Generation is driven entirely by the
semantic type, so supporting a new kind of field means adding a member and a
renderer.

Every inference carries a `confidence` and the `evidence` behind it, because
a guess you cannot audit is worse than no guess. Signals are ranked by how
much they deserve to be trusted:

| Signal | Weight | Why |
| --- | --- | --- |
| `autocomplete` token | 0.97 | Spec-defined and written on purpose |
| Control type (`email`, `url`, `checkbox`) | 0.90 | Fully determines the meaning |
| Field name | 0.80 | Reliable, but names are informal |
| Label text | 0.74 | Weaker — "Policy Effective Date" is a date, not a policy number |
| Option shape | 0.72 | A list of 50 two-letter codes is a state |
| Control type (`date`, `tel`, `number`) | 0.55 | Narrows to a family, does not pick a member |

Two rules make the ranking behave. A **name** rule always beats a **label**
rule, so `effective_date` is a date even when its label says "Policy". And a
specific reading **refines** a general one rather than competing with it, so
`type="date"` plus a name of `date_of_birth` agree on `date_of_birth` instead
of the broader signal winning on weight.

`inspect` marks anything below 0.7 with `?` so a person can look at it:

```
  == Addresses ==
    home_street                  street_address       0.98  required group=home
    home_state                   state                0.99  required group=home 8 options
    home_postal_code             postal_code          0.98  required group=home
```

## Two ways in

**An HTML page.** `extract` finds every control, pairs it with its label
(`for`, a wrapping `<label>`, `aria-label`, or the text just before it),
folds radio groups into a single field with options, reads the declared
constraints, and splits the form into screens from `data-screen` or a
`.screen` section. See `examples/claims_intake.html` — 45 fields over 4
screens.

**A field spec**, for teams who cannot hand over their markup at all. Same
schema out, written by hand:

```json
{"name": "patient_city", "label": "City", "group": "patient",
 "required": true, "max_length": 40}
```

Anything stated explicitly is taken as given — including `semantic_type`,
which inference will not overrule — and anything left out is inferred exactly
as it would be from HTML. See `examples/patient_registration.fields.json`,
and `examples/member_enrollment.fields.json` — 31 fields over 3 screens,
written to exercise the autofill rules below. Extracted schemas can be
edited and fed straight back in.

## Generated values respect the form

A value the target form would reject is not sample data, it is a bug. So
the renderer reads the constraints rather than working around them:
`maxlength` and `minlength` are honoured, a `<select>` only ever receives one
of its own option values, `min`/`max`/`step` bound the numbers, and dates
come out in the format the control or placeholder implies (`YYYY-MM-DD` for a
native date input, `MM/DD/YYYY` when the placeholder says so). A phone takes
the shape its placeholder shows. A nine-character SSN box gets nine digits, a
one-character "Middle Initial" box gets one letter, and a **required**
checkbox is always ticked, because that is what `required` means on a
checkbox.

`--check` verifies all of that before anything is written, and then checks
the harder question — whether the record describes one consistent entity:

```bash
$ python -m fillerai generate schema.json -n 200 --seed 42 --check -o data.json
checked 200 records: no problems
```

It catches a city that does not belong to its state, a ZIP that does not
belong to its city, a title that contradicts the gender beside it, and an
address that is only half filled in. That last one matters: an optional
address block is present or absent as a whole, because a city with no state
is exactly the kind of half-record a model must not learn from.

## Identifiers cannot belong to a real person

By default every identifier is drawn from a range that is real enough to pass
validation and impossible to collide with a real person:

- **SSNs** use area numbers 900–999, which the SSA has never issued.
- **Phone numbers** use a genuine area code with `555-0100`–`555-0199`, the
  block reserved for fiction.
- **Emails** use the RFC 2606 documentation domains, which cannot receive mail.
- **Card numbers** are Luhn-valid on the networks' published test IINs.
- **Routing numbers** satisfy the ABA checksum; **IBANs** satisfy mod-97.

Pass `--realistic-identifiers` to use full real-world ranges when a
downstream validator needs them and the output stays in a controlled
environment.


## Learning to fill the form

The dataset is the training data and the schema says which column means
what, so the model never has to guess at the shape of the form in front of
it. Given the first few fields an agent types, it predicts the rest — and,
as often as not, declines to.

```bash
$ python -m fillerai train schema.json data.csv --seed 1 -o model.json
claims_intake: Conditional tables learned from 450 records, 150 held back to measure
  ...
  == addresses ==
    home_city                    other fields     0.69  from home_postal_code, home_state
    home_state                   other fields     1.00  from home_city, home_postal_code
    mailing_country              the usual value  1.00  nearly always US
  == incident ==
    claim_number                 you              0.00  different in every record
    incident_date                you              0.00  different in every record, shaped like 9999-99-99

  ask the agent for: home_postal_code, employer_city, mailing_city
```

Four things can answer for a field, and the fourth is a real answer rather
than a failure:

**A rule.** A full name is its parts joined up, a middle initial is one
letter of the middle name, an age is arithmetic on a date of birth, a
"same as above" block is a copy. Counting co-occurrence would approximate
these badly and only for values it had already seen; a rule is exactly right
on a name that appears once. Rules are *proposed* from the schema's semantic
types and then **checked against the data**, and kept only if they held. A
field labelled "Full Name" that turns out to hold an account handle fails
its check and is dropped. A rule is never proposed across a group boundary,
so the spouse's surname cannot end up on the applicant's first name.

**What other fields imply.** This is [the selectable part](#choosing-how-it-learns) —
five algorithms, all in plain Python. The default counts how each field's
values co-occur with each other field's and lets the worthwhile predictors
vote, each weighted by how much it has been shown to help and by how many
records back the particular bucket being read.

**What the field usually says.** Always mixed in at a low weight, so thin
evidence backs off to the common answer instead of committing to a bucket of
two rows. On its own it only answers when there really is a usual value —
"United States" in every record is an answer, and a first name whose
commonest value covers 2% of them is not.

**Nothing.** A claim number is different in every record and no amount of
data changes that. The model says so, and says what shape the value takes,
because "you will have to type this" is what an agent needs to hear.

### Why a weighted vote and not naive Bayes

Multiplying independent likelihoods is the textbook move and it is wrong
here. Form fields are not independent — city, state and ZIP are three views
of one fact — so multiplying counts the same evidence three times and
returns 0.999 for answers that are merely popular. A weighted average cannot
do that: with every voter agreeing, the result is still just their shared
probability. The number stays interpretable as a confidence, which matters,
because the whole point is telling an agent what to check.

### How a predictor earns its weight

Not by accuracy. On a form where 94% of records say "United States", *every*
field looks like a brilliant predictor of country, because always guessing
the majority is already right 94% of the time. The measure used instead is
Goodman and Kruskal's lambda — the share of guessing errors that knowing the
predictor removes — which scores that case at zero, correctly.

Lambda has a second failure, and it is the dangerous one. Scored on the rows
its own counts came from, a field with a nearly unique value per record gets
a perfect score for memorising: every bucket holds one row, and every bucket
predicts that row exactly. Two corrections, and both are needed:

| Correction | What it stops |
| --- | --- |
| **Leave one out** — each row scored against its bucket with itself removed | A bucket of one, the memoriser's whole trick, is worth nothing |
| **Beat a shuffle** — the same score recomputed with the predictor column shuffled | Buckets of two or three that agree by luck, which on a five-value field happens often |

Whatever the shuffled column scores is what bucket structure alone is worth,
and the real column has to beat it. Together these are the difference
between a model that knows a ZIP implies a city and one that believes a
diagnosis code implies a courtesy title — which is exactly what the first
working version believed.

A tie inside a bucket counts as a fraction of a hit rather than a clean
miss, because a coin flip between two values is worth half a prediction.
Scoring ties as misses looks stricter and is not: on a target whose values
are all about as common as each other, the no-predictor baseline is one long
tie, and zeroing it hands every predictor credit for beating nothing.

### Confidence that means something

Every prediction carries a number, and the number is measured rather than
asserted. A quarter of the records are held back from the fit; the model is
then asked to finish them from one, two, three, five and eight of their own
fields, and how often it was right at each score is recorded. That curve is
what turns a raw score into the confidence a user reads.

```bash
$ python -m fillerai evaluate model.json unseen.json
  seeds: home_postal_code, employer_city, mailing_city
  from 3 field(s) across 200 records: filled 8% of the remaining 7011
  at 100% accuracy; 5433 had no answer to give

  does the confidence mean anything?
    said 0.90-1.00 -> right  99.8% of 580
    said 0.00-0.55 -> right  48.6% of 998
```

The second line is the one that matters. Everything the model was confident
enough to fill, it got right; everything else it left alone and said so.

Sweeping the number of given fields is deliberate: confidence has to mean
the same thing on the second field of a form as on the thirtieth. The curve
is forced never to go down, by pooling adjacent violators — a dip in the
middle is an artefact of a thin bin, not a finding.

Coverage is reported beside accuracy, and the declined fields beside both. A
model that answers three fields perfectly and refuses the other forty is not
a good autofill model, and one measured only on what it chose to answer
would look like one.

### Which fields to ask for

`train` ends by naming the fields worth asking an agent to type first. It
picks them greedily, which is the right shape here: the value of a set of
seed fields is how much of the form they cover between them, and that grows
by ever less as fields are added, so taking the best next one each time
lands close to the best set for a fraction of the work. Fields a rule
already produces are never suggested — typing a full name that derives from
two boxes you are typing anyway is wasted effort.

### Choosing how it learns

The model is not one algorithm, and only one part of it is up for choice.
Rules are arithmetic, and an age that is the date of birth's arithmetic is
that whichever algorithm is selected. The marginal floor is counting. What
sits between them — *given the fields an agent has typed, what are the
others?* — is the **engine**, and there are five of them.

```bash
$ python -m fillerai algorithms
  statistical  Conditional tables (statistical)
  tree         Decision tree
  forest       Random forest
  nearest      Nearest records
  bayes        Naive Bayes
```

| | what it does | where it wins |
|---|---|---|
| **statistical** | counts how each pair of fields co-occur and lets the worthwhile ones vote | every number in it can be checked by hand |
| **tree** | one decision tree per field, split by gain ratio | relationships that need two fields *together*; you can read the tree |
| **forest** | ten trees per field, resampled and averaged | usually the most accurate, and least fussy about which fields get typed |
| **nearest** | keeps a sample of records and copies the closest matches | closest to how an experienced agent actually works |
| **bayes** | multiplies the evidence under an independence assumption | fast; a clear demonstration of what the calibration step is for |

Everything around the choice is shared, and that is the interesting part.
The same profiling, the same verified rules, the same quarter of the records
held back, the same calibration measured against them, the same scoring. So
a confidence of 0.8 means the same thing whichever engine produced it:
changing the dropdown changes the model, not the yardstick. It is also what
keeps naive Bayes usable — its raw scores really are overconfident, and the
calibration is *measuring* them rather than trusting them.

#### Why the trees exist

A vote of one-field opinions cannot represent a relationship that lives in a
*combination*. "Policy type is family **and** the claimant is a dependent,
therefore the relationship box is child" moves neither field's own table
much, and averaging two weak opinions keeps them weak. On data built so that
exactly that is the only relationship present:

```
statistical  -> 'self'   confidence 0.46   no field seen so far narrows this
tree         -> 'child'  confidence 0.98   role = dependent then policy = family
forest       -> 'child'  confidence 1.00   policy = family then role = dependent
```

That is the whole argument, and it is a test (`tests/test_algos.py`) rather
than a claim.

Three things about growing a tree here are not the textbook default:

- **Gain ratio, not information gain.** A field with a distinct value in
  every record splits the rows into buckets of one, every bucket is pure,
  and plain information gain calls that perfect. It is the same memorising
  failure lambda guards against, in a different costume. Dividing by the
  entropy of the split makes a 300-way split earn its width.
- **Branches are capped and rare values pooled**, which also gives the tree
  somewhere to send a value it has never seen.
- **A missing feature descends every branch at once.** This one matters
  most. A classifier normally has all its features; this one is asked to
  finish a form from whichever three boxes an agent typed first. Stopping at
  the first unanswered question would make the root split the only one that
  ever fires, so an unanswered question is answered by every branch in
  proportion to the rows behind it — Quinlan's fractional-instance rule — and
  a deeper question the agent *did* answer still gets asked.

#### Which one suits this form?

That is a question about the form, so the tool measures rather than
advising. `--compare` fits every algorithm on the same records, with the
same split, the same seed and the same held-out rows:

```bash
$ python -m fillerai train schema.json data.json --seed 1 --compare
claims_intake: 300 record(s), 5 algorithm(s), same split for each

  algorithm       fills  right  offers  rules     fit  asks for
  statistical        6%   100%     20%      0    0.8s  home_city, employer_city
  tree               4%   100%     20%      0    0.8s  home_city, last_name
  forest             2%   100%     28%      0    1.5s  home_state, deductible_amount
  nearest            2%   100%     15%      0    1.0s  date_of_birth, effective_date
  bayes              2%   100%     28%      0    0.9s  deductible_amount, last_name

  best on this form: statistical
```

Those numbers are low because the records are invented; see
[below](#what-this-does-to-synthetic-data-honestly).

### Watching it work, and taking the script away

A fit over a wide form takes a few seconds, and a few seconds of a blank
screen is indistinguishable from a hang. Every stage says what it is doing
as it does it — in the UI as a live log, on the command line with `-v`:

```bash
$ python -m fillerai train schema.json data.json -a forest --seed 1 -v
[  0.00s] == Random forest: 300 record(s), 45 field(s) on the form
[  0.00s]      225 to learn from, 75 held back to measure
[  0.02s] == profiling every writable column
[  0.02s]      1 constant, 21 enumerable, 22 open
[  0.02s]      22 field(s) are different in every record and nothing can predict
                them: date_of_birth, ssn, email, phone, ...
[  0.46s] == proposing rules from the field meanings, and checking each against
             the records
[  0.69s] == growing 10 tree(s) per field over 22 fields, at most 5 deep
[  0.69s]      home_city: removes 30% of the guessing, asking about home_state
[  0.69s] == calibrating confidence on 75 unseen record(s)
[  1.52s] ok measured on 2367 prediction(s) the model had never seen
[  1.53s] == done: 14 of 44 field(s) can be answered, 30 are the agent's to type
```

Beside the log, each algorithm states its **recipe** — its own steps, in
order, in words — and every run hands back a **script**: real Python, no
placeholders, that reproduces the run using nothing but the public API. The
seed is written into it for that reason. `--script run.py` writes it from
the command line; the UI shows it under the log it produced. It is generated
from the same options object the run was given, so a line of it cannot drift
out of date.

A tree can also just be read:

```bash
$ python -m fillerai train schema.json data.json -a tree --tree relationship
  the tree for relationship

    role?  [180 records, says self (53%)]
        claimant: policy?  [88 records, says self (51%)]
            family: self (100%)  [45 records]
            single: spouse (100%)  [43 records]
        dependent: policy?  [92 records, says self (55%)]
            family: child (100%)  [41 records]
            single: self (100%)  [51 records]
```

### The library: what a run left behind

Each stage used to hand its output to the next one and then forget it. That
is fine for one pass and useless for the way the work actually goes —
generate a thousand records, train four models on them, come back tomorrow
and want the one that was good.

So every stage writes what it produced into a library, and every entry
records what it was made from. A model points at the dataset it learned
from, the dataset at the schema it was generated for, the schema at the
markup it was read out of.

```bash
$ python -m fillerai library list
/work/.fillerai

  mdl-20260920-144219841-f145  model   2026-09-20 14:42  claims_intake: forest
      algorithm forest, trained_on 225, fills 6%, right 100%
  dat-20260920-144124282-dcf1  dataset 2026-09-20 14:41  claims_intake: 300 records
      records 300, seed 5, blank_rate 0.12
  sch-20260920-144124f39-76db  schema  2026-09-20 14:41  claims_intake
  src-20260920-1441243b3-5181  source  2026-09-20 14:41  claims_intake.html

$ python -m fillerai library show mdl-20260920-144219841-f145
  made from
    src-...  source   claims_intake.html
      sch-...  schema   claims_intake
        dat-...  dataset  claims_intake: 300 records
```

Follow the chain up and you have the provenance of any model; follow it down
from a source and you have every model descended from it. That is the going
back and forth — the same links read in either direction. In the UI it is
the **Library** tab, and opening an entry restores the whole chain behind
it: a model arrives with its dataset and its schema in place, so the Train
and Simulate stages work immediately.

It is files, not a database: one directory per kind, two files per entry, a
small one with the metadata and a large one with the payload. Listing reads
only the small ones. There is no index to rebuild, no lock to take, and a
person can go and look at any of it with `cat`. It lives in `./.fillerai` by
default; `$FILLERAI_HOME` or `--library PATH` says otherwise, and
`library prune` drops the old ones without ever breaking a lineage.

### What this does to synthetic data, honestly

Every generated record is an independent imaginary person, so there is no
cross-record habit to learn — no "most claims in this region are auto
claims". What the model can learn from generated data is the structure
*inside* a record, and it does. `examples/member_enrollment.fields.json` is
there to show it:

```bash
python -m fillerai generate examples/member_enrollment.fields.json \
    -n 600 --seed 42 -o data.json
python -m fillerai train examples/member_enrollment.fields.json data.json \
    --seed 1 -o model.json
```

```
    member_middle_initial        a rule           1.00  from member_middle_name
    member_full_name             a rule           0.97  from member_first_name, member_middle_name, member_last_name
    member_age                   a rule           1.00  from member_date_of_birth
    home_city                    other fields     0.71  from home_postal_code, home_state
    home_state                   other fields     1.00  from home_city, home_postal_code
```

The full name rule sits at 0.97 rather than 1.00 because some of the
generated names carry a suffix the three name boxes do not — which is the
measurement doing its job.

A real dataset is where the rest comes from. Corporate email follows one
pattern, "mailing address same as home" is a copy, and a claim type really
does predict a diagnosis group. Those are exactly the relationships the
rules and the lambda search look for, and none of them exist in data
invented one person at a time — which is why a model trained on synthetic
records declines so much of the claims form, and is right to.

## The UI

```bash
python -m fillerai serve --open
```

A local web app on the same engine, so nothing here can drift from the CLI —
every button is one call into the same functions. It binds to `127.0.0.1` by
default and asks for a login; the first time it starts it makes an
administrator and prints the password once. `--no-auth` turns all of that off
and gives back the single-user tool — and only works on localhost, because
that is the only place it is the right thing.

Five stages across the top, and a library beside them:

1. **Source** — paste markup, drop a file, or start from a bundled example.
   A field spec works here too.
2. **Schema** — every field with what it means, its group, its constraints and
   how sure inference was. Anything below 0.7 is highlighted. **Correcting a
   field here is taken as final** — the edit sets confidence to 1.0, which
   inference treats as authoritative and will not overrule.
3. **Generate** — count, seed, blank rate and the identifier safety switch,
   then a live preview and CSV/JSON/NDJSON export. Records are checked before
   you see them, and any problem is listed rather than hidden.
4. **Train** — pick how it learns from the five algorithms, with that
   algorithm's own settings and its own account of what it is about to do
   beside the picker. **Show the script** prints the Python that would
   reproduce the run before you start it; **Train** runs it and streams the
   log as it happens, stage by stage, with the script appended underneath
   when it finishes. Then see what the model can and cannot do, read the
   tree it grew if it grew one, and type into the fields it asks for to
   watch the rest of the form fill in underneath, each value with its
   confidence and its reason. The model can be saved as the same JSON file
   the CLI reads.
5. **Simulate** — the form itself, drawn from its own schema. Type the first
   few boxes and the rest fill in underneath as you go, each with its
   confidence and its reason, while the panel beside it counts what that
   saved against filling the same form by hand.
6. **Library** — every source, schema, dataset and model these runs have
   produced, newest first, each with the chain it came from spelled out
   underneath. Open one and the whole chain behind it is restored. Download
   or delete any of it; deleting takes what was made from it too, and says
   so first.

Beside them sits whoever is signed in. That opens **Your account**: change
your own password, and, if you are an administrator, everybody else's
accounts and the database they are kept in.

The server holds the trained model and answers as you type, rather than
shipping a few hundred kilobytes to the browser and taking it back on every
keystroke. It keeps the last few; if yours has fallen out, or the server
restarted, the panel says to train again rather than showing a stale table —
or you can reopen it from the Library, which is where it was kept.

Training runs on a worker thread and the browser polls for new log lines,
rather than holding a stream open. That is the duller choice and the right
one: a fit is seconds rather than minutes, a poll that arrives late costs
nothing, and a tab closed mid-run leaves a thread finishing quietly instead
of a half-written response nobody reads.

## Simulating an agent at work

```bash
python -m fillerai simulate model.json -n 50 --seed 7 --show
```

The other three phases are machinery. This one is the question they were
built to answer: put the model in front of a form nobody has seen, type the
few boxes it asks for, and find out how much of the rest an agent still has
to fill in themselves.

**The form is rebuilt from the schema, not from the markup it came from.**
That is a deliberate trade. Putting the original markup back on screen would
keep its exact styling, but the markup is pasted by the user, and putting it
into the page means running whatever came with it; a sandbox strict enough to
be safe is also strict enough that the simulator could not type into it.
Rebuilding also means this stage works identically for a form that arrived as
a field spec — which is the case that exists precisely because the real
markup could not be shared. What is replayed is the form's substance: the
same fields, in the same order, on the same screens, with the same dropdowns
and the same limits, drawn by this application rather than by the source
page.

**The forms it works are ones the model has never been trained on.** Each is
generated fresh from the model's own schema, blanks and all, because a form
the model has already learned would flatter every number on the page. The
form's real contents are therefore known, which is what lets each filled
value be marked right or wrong as it appears.

**Every field lands in one of four states**, and the last two are results
rather than gaps:

| | |
| --- | --- |
| *you typed* | the agent entered it |
| *the model filled* | confident enough to put in the box |
| *held back* | the model has an answer, below the bar to use it. Shown beside the box with a button, and still counted as the agent's to type |
| *yours* | the model declines. A claim number is different in every record, and saying so is the honest answer |

### What it saved

The saving is the whole argument for the project, and an argument needs a
number. That number cannot be measured from here — nobody is being timed — so
it is modelled, and `fillerai/simulate/effort.py` holds every assumption in
one place rather than burying them:

| Assumption | Default |
| --- | --- |
| Typing speed on form data | 5 characters a second |
| Moving to the next field | 1.2s |
| Choosing from a dropdown | 2.0s, whatever the value |
| Reading a filled value and accepting it | 0.6s |
| Noticing a wrong one, before retyping it | 1.0s |

Two costs are counted per field, because they answer different questions.
Keystrokes are exact and dull: a claim number is fourteen characters whoever
types it. Seconds are the estimate anyone actually cares about, and most of a
dense form's time is not typing at all — it goes on moving between forty
boxes, which is why the per-field overhead sits beside the typing rate rather
than being folded into it.

Three things keep the number from flattering itself:

- **Reviewing a filled value is charged, not free.** The agent is still
  responsible for what they submit.
- **A wrong fill costs more than an empty box.** It is charged the full price
  of typing the field plus the time to notice it was wrong, which is the same
  reasoning behind the model declining a field it cannot predict.
- **A field nobody filled cancels out.** It costs the same on both sides of
  the comparison, so it can never appear as a saving.

When a model fills badly enough, the total comes out negative, and the report
says "3% more" rather than "-3% less". A number the reader has to decode is a
number that will be misread.

One form is an anecdote, so the panel and the CLI both run the same
simulation over many fresh forms and report the average. That is the figure
worth quoting to anyone deciding whether to put this in front of an agent.

### What it says about synthetic data

Run it on `claims_intake.html` with a model trained on generated records and
it fills two or three boxes out of forty-four, for a saving of a few percent.
That is not the simulator being pessimistic; it is the simulator doing its
job, and it puts a number on what [the section above](#what-this-does-to-synthetic-data-honestly)
argues.

Thirty of that form's fields are different in every record — names,
identifiers, free text, amounts — and no amount of training data makes those
predictable. Of the fourteen that remain, the model fills the ones that are
genuinely there: a state follows its city, a country is the same in every
record. What it cannot find is everything a real history would carry —
corporate email built from a name, a mailing address copied from a home one,
a claim type that really does imply a diagnosis group — because records
invented one persona at a time have none of those habits to learn from.

So the honest reading of this stage is that it measures two things at once:
how good the model is, and how much the training data was worth. The panel
says so in as many words when the saving comes out small, rather than leaving
a bare 3% on screen to be misread as a verdict on the idea. Point phase 3 at
real past submissions and this is the number that moves.

## Accounts, and where everything is kept

Until now FillerAI was one person on one machine: no login, and a directory
of JSON called `.fillerai` holding every source, schema, dataset and model.
That is still exactly what `--no-auth` gives you. But the moment a second
person opens the same URL, two questions have to be answered — whose library
is this, and who is allowed to hand out accounts — and a directory of files
cannot answer either.

```bash
# The first start makes an administrator and prints the password once.
python -m fillerai serve --open

# Or set it up from a shell first, which is also the way back in if
# nobody can sign in any more.
python -m fillerai users add krishna --admin
python -m fillerai users add dana                  # password generated, shown once
python -m fillerai users list
python -m fillerai users disable dana              # keeps everything they made
python -m fillerai db status
```

### The database

SQLite, through `sqlite3` in the standard library, so the promise the rest of
this project makes survives the change: nothing to install, nothing that
leaves the machine. One file next to the library it replaces
(`.fillerai/fillerai.db`), which `sqlite3` will open and read.

Postgres is the stated next step, and the shape for it is already here.
Everything goes through `fillerai.db.Database`, and what is actually specific
to a backend is small enough to name:

- **The parameter style.** Callers write `?`; a backend that wants `%s`
  rewrites the statement on its way through, so no query above `fillerai.db`
  changes.
- **Connecting.** A URL in, a DB-API connection out.
- **Nothing else.** The schema is written in the SQL both accept — `TEXT`,
  `INTEGER`, `CREATE TABLE IF NOT EXISTS` — and times are stored as ISO 8601
  strings rather than as a timestamp type, because the two disagree about
  timezones in a way that is not worth a translation layer.

So `postgresql://...` is one subclass, not a rewrite. Asking for one today
says that in as many words rather than failing with a missing driver.

Migrations run on every start and do nothing when there is nothing to do, so
a database made by an older build catches up by itself.

### Your library is yours

Every entry carries an owner. Alice's fifty models are noise in Bob's list,
and a model trained on a form Bob was never shown is not Bob's to open — so
the whole library API is scoped to whoever is asking, and an id from somebody
else's library reads as "there is nothing called that", because as far as
that person is concerned there is not.

An entry is named by its owner and its id together, not by its id alone.
That is what lets two people import the same directory library and both keep
the ids their lineage and their notes refer to.

**Nothing you already have is orphaned.** The first start with a database
copies an existing `.fillerai` directory into the first administrator's
library, ids and lineage intact, and says so. Running it again copies
nothing, because every id is already there. The same thing by hand:

```bash
python -m fillerai db import --from ./.fillerai --user krishna
```

The directory is only read, never changed, so the CLI's `library` commands go
on working against it exactly as before.

### Scripts are kept too

The Python a training run is, generated from the options that ran it, is now
stored beside the model it produced. That looks redundant right up until a
default changes: then the script in the library is what that model was
actually trained by, and what today's code would write is not. It is a fifth
kind in the library, it downloads as `.py` rather than as JSON with the
Python inside a string, and it goes when its model goes.

### How the login works

- **Passwords** are hashed with `hashlib.scrypt` at the parameters usually
  called interactive — about 45ms and 16MB per attempt, which is nothing to a
  person signing in and a great deal to somebody working through a stolen
  table. PBKDF2 is the fallback for a Python built without scrypt, and both
  are recognised on the way back in.
- **Sessions live in the database**, not in the cookie. The cookie carries a
  random session id and a signature over it; the signature is not what makes
  the session safe, it is what lets a forged cookie be thrown out without
  touching the database. Signing out deletes the row, so it takes effect
  everywhere at once — which a self-contained token cannot promise.
- **The cookie is HttpOnly and SameSite=Strict**, and every call carries a
  token in a header that another origin cannot read. A cookie alone would let
  any page on the machine POST here with your credentials attached.
- **A wrong password and an unknown username read identically**, and take
  about the same time, because the pair of messages people usually write here
  is a way of asking the server which usernames exist.
- **Six wrong guesses earn a fifteen-minute pause**, which clears itself.
- **A password handed to you by somebody else has to be changed** before
  anything else works, so nobody is working in an account another person
  knows the password to.
- **The last administrator cannot be removed** — not disabled, not demoted,
  not deleted. A shared tool that can be locked out of itself by one careless
  click is a support call, and the check costs one query.

Two roles, and deliberately two: every third role anybody proposes turns out
to be a permission, and permissions belong to whatever they guard. An
administrator creates accounts, changes roles, resets passwords and turns
accounts off; everybody else does the work. Deleting an account takes its
library with it, which the panel says — with the number of entries — before
it asks.

## Options

| Flag | Default | What it does |
| --- | --- | --- |
| `-n, --count` | 10 | How many records |
| `--seed` | none | Makes the run reproducible; each record is seeded from it, so a single record can be reproduced on its own |
| `-f, --format` | json | `json`, `ndjson` or `csv` |
| `--blank-rate` | 0.12 | Share of optional fields left empty, so training sees the partly-filled forms it will meet in production. `0` fills everything |
| `--check` | off | Validate and check coherence before writing |
| `--include-persona` | off | Attach the entity behind each record, for debugging |
| `--realistic-identifiers` | off | Use real-world ranges instead of the reserved ones |

`train` takes:

| Flag | Default | What it does |
| --- | --- | --- |
| `-a, --algorithm` | `statistical` | Which engine learns the relationships |
| `--set KEY=VALUE` | — | A setting for that algorithm; repeatable. `fillerai algorithms` lists each one's |
| `--compare [NAME ...]` | — | Fit every algorithm on the same records, same split, and print them side by side |
| `--no-rules` | off | Skip the verified rules, to see what the algorithm manages on its own |
| `-v, --verbose` | off | Print each stage of the fit as it happens |
| `--script PATH` | — | Write a runnable script that reproduces this run |
| `--tree FIELD` | — | Draw the tree grown for one field |
| `--holdout` | 0.25 | Share kept back to measure confidence on |
| `--seed` | none | Makes the run reproducible |
| `--ask` / `--seeds` | 3 | How many seed fields to suggest, or which |
| `--evaluate PATH` | — | Score the result on a second dataset |
| `--save` | off | Keep the model in the library |

It reads `.json`, `.ndjson` and `.csv` alike — a CSV has lost the difference
between `false` and the string `"false"` by the time it is on disk, which is
fine, because every value is normalised the same way before it is counted.

`algorithms` lists what can be selected, with `--recipe` for each one's
steps in order. `library` has `list`, `show`, `export`, `delete` and
`prune`; `extract`, `generate` and `train` take `--save` to put what they
produced into it, and every command that touches it takes `--library PATH`.

`users` has `list`, `add`, `passwd`, `role`, `disable`, `enable` and
`delete`; `db` has `status` and `import`. Both take `--database URL`
(`sqlite://<path>`, or `$FILLERAI_DATABASE_URL`) and `--library PATH` for
the directory the default database sits in. `add` and `passwd` generate a
password and show it once when none is given, and that password has to be
changed at first sign-in. `delete` refuses an account with a library behind
it until `--yes` says that the library goes too.

`predict` takes repeated `--set field=value` and `-f text|json|record`;
`record` gives the filled form on its own, ready to post back. Both it and
`evaluate` take `--threshold`, the confidence at which a prediction is
offered without a warning. It defaults to 0.7 on purpose: on a claims form a
wrong value that looks filled in costs more than an empty box.

`simulate` takes `-n/--forms` (default 25) for how many fresh forms to work
through, `--records` to use a dataset you already have instead of generating
them, `--ask`/`--seeds` and `--threshold` as above, `--seed` to make the
generated forms reproducible, `--show` to print the first form field by
field, and `-o` for the full report as JSON.

`serve` takes `--port` (default 8000), `--host` (default `127.0.0.1`),
`--open` to launch a browser, `-v` to log each request, `--library PATH` for
where the UI keeps what it produces, `--database URL` for where the shared
data lives, and `--no-auth` for no login and no accounts — one library, for
one person on one machine. `--no-auth` is refused anywhere but localhost:
without accounts everyone who can reach the port is signed in, and a printed
warning is the wrong answer to that, because the person who needs to read it
is already not reading the console.

Read-only and disabled controls are skipped — those are the form's to fill,
not ours.

## As a library

```python
import fillerai

schema = fillerai.extract_html("examples/claims_intake.html")
dataset = fillerai.generate(schema, count=500, seed=42)

assert fillerai.validate(schema, dataset.records) == []
assert fillerai.coherence_report(schema, dataset.records) == []

model = fillerai.train(schema, dataset.records, seed=1, algorithm="forest")
print(fillerai.suggest_seed_fields(model, 3))

for name, guess in model.predict({"home_postal_code": "78701"}).items():
    if guess.confidence > 0.8:
        print(name, guess.value, guess.because)

unseen = fillerai.generate(schema, count=200, seed=99).records
print(fillerai.evaluate(model, unseen, seeds=3).headline())

# And what all of that is worth to someone filling the form.
from fillerai.simulate import run, sweep

seeds = fillerai.suggest_seed_fields(model, 3)
print(sweep(model, unseen, seeds).headline())

case = unseen[0]
print(run(model, {n: case[n] for n in seeds}, case=case).headline())
```

For anything beyond the algorithm's name — its own settings, a trace to
watch the run, turning the rules off — build the options yourself:

```python
from fillerai.train import TrainOptions, train
from fillerai.train.trace import Trace

watching = Trace(echo=True)          # prints each stage as it happens
model = train(schema, dataset.records, TrainOptions(
    algorithm="forest",
    tuning={"trees": 16, "max_depth": 5},
    seed=1,
    trace=watching,
))
print(model.engine.drawn("home_state"))   # the tree, as text
```

And the library is a plain object over a directory:

```python
store = fillerai.Store(".fillerai")
schema_entry = store.save_schema(schema)
data_entry = store.save_dataset(dataset.records, parent=schema_entry.id)
model_entry = store.save_model(model, parent=data_entry.id)

[e.kind for e in store.lineage(model_entry.id)]
# ['schema', 'dataset', 'model']
```

A model saves as JSON and loads back exactly as it was
(`AutofillModel.from_json`), and fitting twice on the same data gives a
byte-identical file — a model you cannot diff is a model you cannot review.
A model written before the engine became selectable still loads: it was
necessarily fitted with conditional tables, so its links are read straight
into that engine.

## Layout

```
fillerai/
  schema.py            the versioned field-schema contract
  infer.py             semantic type from ranked evidence
  cli.py               extract / generate / train / predict / simulate / serve
                       / algorithms / library / users / db
  store.py             the library in a directory: what each run produced
  db.py                the database, and the interface a backend meets
  dbstore.py           the same library in the database, with an owner
  auth.py              users, passwords, roles and sessions
  extract/
    dom.py             a minimal DOM over html.parser
    html_form.py       HTML -> schema
    spec.py            field spec (or a saved schema) -> schema
  generate/
    catalogs.py        coherent geography, names, companies
    persona.py         one consistent entity per record
    render.py          persona + field -> a value the form accepts
    dataset.py         records, output formats, validation
  train/
    features.py        what each column turned out to look like
    associate.py       what one field tells you about another
    derive.py          rules, proposed from the schema, checked on the data
    model.py           fit, predict, calibrate, save - the shared layer
    evaluate.py        score on unseen records; choose the seed fields
    trace.py           the running commentary a fit gives while it works
    script.py          the run, written out as a script that reproduces it
    algos/             the selectable engines
      base.py          what an algorithm has to be, and the shared vote
      statistical.py   conditional tables, combined by a weighted vote
      tree.py          decision trees, and a forest of them
      nearest.py       the most similar past records, weighted per field
      bayes.py         naive Bayes, and what the calibration does to it
      relevance.py     the cheap "which fields matter here" shortlist
  simulate/
    form.py            schema -> a form a browser can draw
    effort.py          what filling a field costs, and every assumption behind it
    run.py             the run loop: the board, the saving, the score
  web/
    server.py          the local HTTP API, one function per endpoint
    static/            index.html, app.js, styles.css - no build step
                       login.html, login.js - the one page served signed out
examples/
  claims_intake.html                  45 fields, 4 screens
  patient_registration.fields.json    21 fields, written as a spec
  member_enrollment.fields.json       31 fields, exercises the autofill rules
  out/                                schema and generated samples
tests/
  test_fillerai.py     extraction, inference, generation, the CLI
  test_train.py        features, associations, rules, the model, calibration
  test_algos.py        the algorithms: the shared contract, and what each is for
  test_store.py        the library, mostly lineage and being asked nonsense
  test_db.py           migrations, transactions, and the backend interface
  test_dbstore.py      the same library in SQL, plus owners and importing
  test_auth.py         passwords, sessions, roles, and the account commands
  test_trace.py        the log, mostly the cursor under concurrent writes
  test_simulate.py     the form, the effort model, the run loop
  test_web.py          every endpoint, over a real socket
  test_web_auth.py     the same server with accounts on: the door, not the stages
```

## Tests

```bash
python -m unittest discover -s tests -v
```

475 tests, no dependencies. They cover malformed markup, each inference rule,
the checksum algorithms, constraint compliance, the coherence guarantees
above, the model's rules and its scoring, the library's lineage, the log's
cursor under concurrent writes, and the web API end to end over a real
socket - routing, path traversal, body limits and every error message the UI
can show.

The account tests are weighted towards the refusals rather than the happy
path, because the happy path is one call and the refusals are the point: a
wrong password and an unknown username reading the same, a cookie somebody
edited, a session that outlived its welcome, a request without its token, one
person reaching for another person's library, and every way of leaving the
tool with no administrator.

The algorithm tests come in two kinds. One is the contract, run over the
registry rather than written out per algorithm, so a new engine has to fit,
predict, serialise, reload, calibrate and report itself without anybody
remembering to add tests for it. The other is about what each algorithm is
*for*: a decision tree that cannot see a two-field relationship is not a
decision tree, however cleanly it round-trips.

Several tests are regressions for bugs found while building this, and worth
keeping for what they say about the sharp edges:

- A falsy-value filter dropped `minimum: 0` when a schema was saved, because
  `0 == False` in Python. A reloaded schema then generated a deductible of
  8897 against a maximum of 5000.
- Picking a birth *year* for a target age made someone born in December come
  out a year younger. The date now comes from an exact-age window.
- The group prefix in `employer_city` matched the "employer" keyword and made
  it a company name. Prefixes are stripped before matching.
- `payee_routing_number` was read as a tax ID, because "rou**tin**g" contains
  "tin". That rule now needs a word boundary.
- Rejecting an over-long request body without reading it left the browser
  with a broken pipe instead of the error message. The body is now drained,
  bounded, before the 413 goes out.
- Scored on its own training rows, `home_postal_code` came out a strong
  predictor of a *different* address's state, purely by memorising: one row
  per bucket, every bucket right. Hence leave-one-out, and the shuffled
  baseline beside it.
- Scoring a tie as a miss zeroed the no-predictor baseline whenever a
  target's two commonest values were neck and neck, which handed every
  predictor credit for beating nothing. Ties are now worth `1/k`.
- Two fields holding the same value verify as copies of each other in both
  directions. The first cycle check dropped both, losing a relationship that
  was real and perfectly usable one way round.
- A prediction's stated reason was read off the marginal separately from the
  value it chose, so wherever two values tied it named a different one. The
  reason is now built from the answer.
- Costing a dropdown by the length of its value made picking
  "Massachusetts" seven times the work of picking "Ohio", and handed the
  simulator a saving that came out of long labels rather than out of the
  model. A picked control now costs the same whatever it holds.
- The saving charged nothing for a wrong fill, which made a model that
  filled boxes badly look better than one that left them empty — exactly
  backwards, and exactly the failure the 0.7 threshold exists to prevent. A
  wrong value is now charged the full price of typing the field plus the
  time to notice it.
- Redrawing the form on every keystroke meant the box being typed into was
  rewritten underneath the caret. The form is drawn once and only its values
  and badges are updated, and the focused control is never written to.
- The nearest-record engine kept every row when the dataset was smaller than
  its cap, which left nothing to score itself against, so every field came
  back rated useless. The rows to score on are now reserved before the index
  is sampled, not after.
- Listing the library sorted by id, and an id starts with its kind, so every
  source sorted above every model and "newest first" was nothing of the
  sort. It sorts by the time inside the id now - and two entries written in
  the same millisecond fell back on their random tails, which is exactly how
  four algorithms trained on one dataset arrive, so ids are also forced past
  the last one within a process.
- The simulator's right/wrong notes were given a `.mark` class, which the
  topbar's brand square already owned - a solid accent-blue block - so every
  verdict came out as white text on a blue tile. Nothing failed and nothing
  logged; the only way to notice was to look at it. A test now asserts that
  no bare class name is defined twice in the stylesheet, because that shape
  is the bug.

## What comes next

Six phases in, the machinery is complete: a form is read, data is invented
for it, a model is fitted by whichever of five algorithms suits it, the run
is watched and kept, the whole thing is played back with a number on what it
saved, and all of it now belongs to somebody who had to sign in. What the simulate stage says about that number is still the
honest place to pick up.

**Real history is the missing input, and it is now the binding one.** With
five algorithms to choose between, the choice barely matters on generated
data — they land within a few points of each other because there is only so
much for any of them to find. A decision tree that can represent "family
policy *and* dependent claimant means child" has nothing to represent when
the generator never put such a rule in. The algorithms were worth building
because real submissions are full of exactly that shape, and the way to find
out is to run `--compare` on a directory of them. Everything the model declines on the
claims form, it declines because records invented one persona at a time carry
none of the habits a real queue of submissions would. The next useful work is
not a better model; it is pointing `train` at a directory of real past
submissions inside the environment that holds them, and running `simulate`
against the same forms to see what moves. Nothing in the pipeline needs
changing for that — a dataset is a dataset — which was the point of keeping
generation and training on opposite sides of the schema.

**Two things the simulation would then be worth extending with.** Per-agent
models, since two people filling the same queue develop different habits and
the model currently averages them together — and with the library recording
which dataset each model came from, keeping one model per agent is now a
matter of keeping the datasets apart rather than a change to anything.
And a cost model calibrated against real timings rather than the stated
assumptions in `fillerai/simulate/effort.py`: every number in the saving is
only as good as those five constants, and a stopwatch on ten real forms
would replace all of them.

**Postgres is one subclass away.** The database interface exists for that
one reason, and the two things a driver changes - connecting, and the
parameter style - are the two things it is allowed to change. What is not
there is the driver, and it will not be until FillerAI is allowed a
dependency, which today it is not. Alongside it, the obvious next thing
accounts want is a record of who did what: the schema has room for it and
nothing needs it yet.

**One thing the library is one step from.** It already knows that four
models descend from one dataset. Showing them as a table in the Library
panel, rather than four rows that happen to share a parent, is the last
piece of making the comparison something you come back to rather than
something you run.

---

## Further reading

[**Training, serving, and what happens at 20,000 records**](docs/training-and-scale.md)
— what a training run does stage by stage and what each stage costs, what it
would take to serve a model against a real production form, and measured
behaviour at 20,000 records including the one classification limit that
silently drops high-cardinality fields.

[**Weight-based training: what it would take, and what it would buy**](docs/weight-based-training.md)
— an analysis, not a change: what a sixth engine that learns weights rather
than counts would cost under the no-dependency rule, measured against the five
that exist, and which of the candidates is worth building.
