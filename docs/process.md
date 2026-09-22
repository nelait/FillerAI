# The process, end to end

What actually happens, in order, from a form nobody has seen to a number
saying what the model saved — and the operational process around it: who runs
it, where the output lands, and how the work gets from a branch to `main`.

[architecture.md](architecture.md) is the same system arranged by component.
This one is arranged by what you do.

Every command below was run against version 0.11.0 while this was written.

---

## 0. The shape of a full pass

```
  extract  ->  generate  ->  train  ->  simulate
   schema      records       model      a costed run
```

Each step takes the previous step's file and writes its own. Add `--save` to
any of them and the output also lands in the library with a pointer back to
what it came from, which is what makes the chain re-readable later.

The whole thing, on the form that ships with the repo:

```bash
python -m fillerai extract examples/claims_intake.html -o claims.schema.json
python -m fillerai generate claims.schema.json -n 500 --seed 42 -o claims.data.json
python -m fillerai train claims.schema.json claims.data.json --seed 42 -o claims.model.json
python -m fillerai simulate claims.model.json -n 50 --ask 3 --seed 7
```

Or `python -m fillerai serve` and do all four in a browser.

---

## 1. Extract — turn a form into a schema

```bash
python -m fillerai extract <source> -o form.schema.json
```

`<source>` is **either** an `.html` page **or** a `.json` field spec. Both
produce the same `FormSchema`, and nothing downstream can tell which it was.

**Which way in to use.** Markup if you have it: it carries the labels, the
option lists, the `maxlength`s and the `autocomplete` tokens, so inference has
the most to go on. A field spec when the markup cannot be shared — which is the
case this project was built for — or when the form does not exist yet.

**A spec can also carry the form's business rules**, and this is the single
highest-leverage thing anyone can do for the quality of the result:

```json
{"name": "deductible", "control": "select",
 "options": ["250", "500", "1000", "2500"],
 "follows": "plan_tier",
 "when": {"Platinum": "250", "Gold": "500",
          "Silver": "1000", "Bronze": "2500"}}
```

`follows` names one field or several; `when` maps their values to what this
field may then hold — one value where the rule decides it, a list where it only
narrows it. With several sources the key joins their values with `|` in the
order `follows` gives. `otherwise` covers a combination the table omits.

These rules are the part of a form a company **can** hand over: they live in
the business manual, not in anybody's submitted record. §8 below has the
numbers on what they are worth.

**Then check what it inferred.** Extraction is the only stage where a silent
mistake propagates through everything after it:

```bash
python -m fillerai inspect form.schema.json --evidence
```

Every field prints its semantic type, its confidence and the evidence that
produced it. `--review-below 0.8` flags the weak ones. A field typed
`unknown` generates as free text and predicts as nothing; a field typed
*wrongly* generates plausible values of the wrong kind, which is worse and
harder to notice. This is the five minutes that decides the run.

**Optionally, ask a model for the rules** (§7).

---

## 2. Generate — invent records the form would accept

```bash
python -m fillerai generate form.schema.json -n 500 --seed 42 --check -o form.data.json
```

| Option | What it is for |
|---|---|
| `-n` | how many records |
| `--seed` | reproducibility — always pass one for anything you will compare |
| `-f json\|ndjson\|csv` | output format |
| `--blank-rate` | share of *optional* fields left empty (default 0.12; 0 fills everything) |
| `--check` | validate the records before writing them |
| `--realistic-identifiers` | real SSN/phone/card ranges instead of the reserved non-issuable ones |
| `--include-persona` | attach the entity behind each record, for debugging |

**How many records.** 500 is a reasonable default for a 40-field form. Below
about 200 the holdout gets too thin for the confidence calibration to mean
much (`MIN_CALIBRATION_ROWS = 20` after a 25% holdout), and learned vote
weights need `COMBINER_MIN_HOLDOUT = 200` held-out rows before they are even
attempted. More records help until the relationships in the data run out,
which on synthetic data happens sooner than you would like (§8).

**Keep the seed.** Two datasets generated without one are not comparable, and
most of what anyone wants to ask of this system is a comparison.

**Verify an existing dataset** against a schema at any time:

```bash
python -m fillerai check form.schema.json form.data.json
```

---

## 3. Train — learn to finish the form

```bash
python -m fillerai train form.schema.json form.data.json --seed 42 -o form.model.json
```

What the run does, in order, and all of it shared across engines:

1. **Profile the columns** (`features.py`) — decide which fields are
   *enumerable* (a closed set to choose from) and which are *open* (essentially
   unique per record). A state dropdown is the first; a claim number is the
   second. No amount of data makes the second predictable, and a model that
   pretends otherwise is worse than one that says "you will have to type this".
2. **Hold a quarter of the records back** (`--holdout`, default 0.25). Nothing
   fitted is ever measured on the rows it was fitted on.
3. **Propose rules from the semantic types, then check them against the data**
   (`derive.py`). A form labelling something "Full Name" that turns out to hold
   an account handle fails its check and is dropped.
4. **Fit the engine** — the selectable part.
5. **Fit the vote weights**, if there are enough held-out rows to spare some,
   and keep them only if they beat the hand-picked ones on rows neither saw.
6. **Calibrate the confidence** on the rest of the holdout: fit how often
   predictions of a given raw score actually turn out right.
7. **Score it**, and suggest which fields to ask the agent for.

### Choosing the engine

```bash
python -m fillerai algorithms            # what each one is, and when
python -m fillerai algorithms --recipe   # each one's steps, in order
python -m fillerai train form.schema.json form.data.json --compare
```

`--compare` fits every engine on the same records and prints them side by
side. That is the honest way to pick one, and it is cheap.

`-a statistical|tree|forest|nearest|bayes|linear`, default `statistical`.
Rough guidance:

- **`statistical`** unless you have a reason. Every number in it is checkable
  by hand, and it is the one an agent can be shown.
- **`tree`** or **`forest`** when the form has rules that need two fields
  *together* — a vote of single-field opinions cannot see those.
- **`nearest`** when past submissions come in recognisable families. Note its
  model file contains a bounded sample of the training *records*, which is
  nobody's business on generated data and very much somebody's on real ones.
- **`linear`** when a high-cardinality field (a city, a broker) is real
  evidence. The other engines drop it; this one hashes it.
- **`bayes`** to see what the calibration step is for. Its raw 0.999s land in
  the top bin and come out at whatever that bin is really worth.

Two flags exist to answer "what is this actually doing":
`--no-rules` (what the engine manages on its own, with no rule answering for
free) and `--no-learned-weights` (what fitting the vote weights is worth).

### Watching it, and taking it away

`-v` prints each stage as it happens. `--tree FIELD` draws the tree grown for
one field. `--script run.py` writes **real runnable Python** that reproduces
the run using nothing but the public API, generated from the same options
object the run was given — so it cannot describe a run that did not happen.
The seed is written into it for exactly that reason.

### Reading the report

Two numbers, and neither is allowed to hide the other. **Accuracy** is how
often a filled value was right; **coverage** is how much of the form the model
was willing to fill at all. A model that answers three fields perfectly and
declines forty is not a good autofill model.

`ACCEPT_ABOVE = 0.7` is the confidence at which a prediction is offered
without a warning — deliberately strict, because on a claims form a wrong
value that looks filled in costs more than an empty box.

---

## 4. Predict and evaluate

Fill the rest of a form from a few fields:

```bash
python -m fillerai predict form.model.json --set home_postal_code=78701 --set plan_tier=Gold
```

Score a model on a dataset it has never seen:

```bash
python -m fillerai evaluate form.model.json other.data.json --ask 3
```

`--ask N` uses the N fields the model itself suggests; `--seeds a,b,c` names
them instead; `--threshold` moves the confidence at which a value is offered.

---

## 5. Simulate — what it actually saved

```bash
python -m fillerai simulate form.model.json -n 50 --ask 3 --seed 7
```

The model is played against forms it has never seen. A few fields are typed
into each, the model fills what it can, and the run is costed against filling
the same form by hand — with the review of every filled value, and the cost of
correcting the wrong ones, charged against the saving.

`--records` plays a real dataset instead of generating the forms.
`--show` prints the first form field by field. `-o` writes the full report as
JSON.

Every report ends with the assumptions that produced it. They are five
constants in `fillerai/simulate/effort.py` and they are the weakest link in
any number this project reports — see [assumptions.md](assumptions.md) §4.

---

## 6. The library — what a run left behind

Add `--save` to `extract`, `generate` or `train` and the output lands in the
library (`./.fillerai` by default, or `$FILLERAI_HOME`, or `--library PATH`)
with a pointer to what it came from.

```bash
python -m fillerai library list                 # newest first
python -m fillerai library show mdl-…           # what it came from, what came of it
python -m fillerai library export mdl-… -o m.json
python -m fillerai library delete mdl-…
python -m fillerai library prune --keep 50
```

`library show` is the one worth knowing: it prints the chain upward
(model → dataset → schema → source) and the entries descending from it, which
is how you find the four models that came off one dataset.

With accounts on, the same library lives in the database with an owner on
every entry. Move a directory library in:

```bash
python -m fillerai db status
python -m fillerai db import
```

Ids and lineage survive the move.

---

## 7. Asking a model for the rules (optional, off by default)

Writing declared rules by hand is what 27 rules on the quote form and 31 on
onboarding cost somebody. This asks for them instead. **Nothing here runs
unless you set a key and run one of these commands.**

```bash
export FILLERAI_LLM_KEY=sk-…            # or ANTHROPIC_API_KEY / OPENAI_API_KEY
python -m fillerai llm status           # which service and model; prints no key
python -m fillerai propose-rules form.fields.json --dry-run   # what it would cost
python -m fillerai propose-rules form.fields.json -o rules.json
python -m fillerai apply-rules form.fields.json rules.json -o form.rules.json
```

The two-step shape is the feature. `propose-rules` asks, runs every proposal
through the validation gate (architecture.md §8), and prints what survived with
its reasoning; `apply-rules` merges the ones you believe and **never
overwrites a hand-written rule**. A model inventing plausible business logic is
exactly what this is, and the reviewer in the middle is the point.

`--max-spend` (default $1) bounds the run, `--dry-run` prices it without
sending anything, `--batch` sets how many fields are decided per call (a large
form is split at 60 by default), `--above` on `apply-rules` filters by
confidence.

**In the UI:** `Settings`, top right, takes a key for either service and says
which one the next run will use. `Propose rules` on the Schema step asks,
shows what survived, and applies only the ones you tick. A key typed there
lives in the server process and is written nowhere.

---

## 8. What the process is worth, measured

Five example forms, each: extract → generate 500 records (seed 42) → train
(default engine, seed 42) → simulate 50 unseen forms typing 3 fields (seed 7).
Run on version 0.11.0; reproduce with the four commands in §0.

| Form | Fields | Declared rules | Filled | Accuracy | Saving |
|---|---|---|---|---|---|
| `claims_intake.html` | 45 | 0 (markup declares none) | 2 of 42 | 100% | **3%** |
| `patient_registration` | 21 | 0 | 3 of 18 | 100% | **7%** |
| `member_enrollment` | 31 | 0 | 3 of 29 | 100% | **7%** |
| `auto_insurance_quote` | 54 | 27 | 17 of 51 | 97% | **23%** |
| `employee_onboarding` | 53 | 31 | 26 of 50 | 98% | **32%** |

"Filled" counts against the fields left after the three the agent typed.

The pattern is the whole argument, and it is not about the model. The top
three forms have no declared rules, so the only structure in their generated
data is what a persona brings with it — an address, a phone, a name — and
addresses are very nearly all the model fills. The bottom two carry the form's
own business rules, so the generator puts real relationships into the data and
the model finds them.

**What this means in practice.** On synthetic data, the ceiling is the rules
you declared, not the engine you picked — the six land within a few points of
each other because there is only so much for any of them to find. The two ways
past it are to declare the rules (§1) and to train on real past submissions,
which needs no change to the pipeline at all: a dataset is a dataset.

---

## 9. Running it for other people

```bash
python -m fillerai serve                # accounts on; makes an admin, prints the password once
python -m fillerai serve --no-auth      # one person, one machine, no login
```

Accounts are on by default. The first start makes an administrator and prints
the password once; `FILLERAI_ADMIN_PASSWORD` sets it instead, and
`python -m fillerai users add` is the way back in if nobody can sign in.

```bash
python -m fillerai users list|add|passwd|role|disable|enable|delete
python -m fillerai tokens list|add|revoke
```

`--no-auth` is the single-user tool this started as, and binds to localhost
only, since anyone who can reach the port is then signed in.

**For an application calling in**, issue a token and point the client at the
service:

```bash
python -m fillerai tokens add alice --name "claims-system" --model mdl-…
```

The token is shown once. Full reference: [integration.md](integration.md).

---

## 10. The development process

**Tests.**

```bash
python -m unittest discover -s tests -q
```

772 tests, no dependencies, entirely offline — the LLM tests replay recorded
exchanges through `RecordedTransport`, which raises on anything unrecorded, so
a changed prompt fails loudly rather than reaching for the network.

**There is no CI in this repository** — no `.github/workflows` — so that local
run is the only signal, and no pull request here will ever show a green check.
Run it before you push. Expect roughly 70 seconds, and see
[pending.md](pending.md) §2.1 for the one test that fails about 1 run in 10
for a reason that is not your change.

**The acceptance gate for the LLM features is separate** and costs money, so it
is outside `tests/` and skips itself unless you opt in:

```bash
FILLERAI_LLM_LIVE=1 FILLERAI_LLM_KEY=sk-… python -m unittest discover -s livetests
```

It has never been run — see [pending.md](pending.md) §1.1.

**Branches and pull requests.** Work happens on a `claude/…` branch off
`main` and lands through a pull request that Krishna merges. A merged branch
is spent: the next piece of work restarts from `main` rather than stacking on
it.

**Two rules that outrank convenience**, and a reviewer should push back on
either:

- **No dependency gets added**, and no build step. `dependencies = []` is
  asserted by a test because it is a promise about where this can run.
- **Nothing in the core imports `fillerai/llm/`** at module scope.
  `tests/test_llm_fence.py` will tell you, three different ways.

**When adding an engine:** one module in `train/algos/`, one `Algorithm`
registered at the bottom of it, one line in `algos/__init__.py`. Nothing above
the package changes, and the shared layer will profile, verify, hold out,
calibrate and score it like the others — which is what makes the comparison
meaningful.

**When adding a database change:** append a step to `MIGRATIONS` in
`fillerai/db.py`. Never edit one that has shipped.

---

## See also

- [architecture.md](architecture.md) — the same system by component.
- [assumptions.md](assumptions.md) — what every number here rests on.
- [pending.md](pending.md) — known gaps and defects.
- [training-and-scale.md](training-and-scale.md) — what each training stage
  costs, and behaviour at 20,000 records.
