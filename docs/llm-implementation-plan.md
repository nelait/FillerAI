# LLM implementation plan

[**LLM-based modelling**](llm-modelling.md) works out *whether* a language
model belongs in this pipeline. This document is the *how*: the module layout,
the four phases in the order they should be built, what each one has to prove
before the next is worth starting, and the two problems that are harder here
than the modelling — keeping a network call out of a package whose whole point
is that it has none, and testing a non-deterministic component inside a suite
of 537 deterministic tests.

**Nothing here is built.** No code under `fillerai/` has been written or
changed. This is the plan; every file path below is a proposal.

The analysis's conclusion is the premise of this plan, so it is worth
restating in one line: **an LLM earns its cost at build time and not at fill
time.** Phases 1 and 2 run once per form design, offline, on field names and
markup. Phase 3 exists only to settle the prediction question on evidence, and
is expected to return a negative result. Phase 4 is the one place a model adds
a capability rather than a percentage, and it is deliberately last.

---

## 0. The two constraints that shape everything

Before any feature, two things have to be true, and they drive the whole
layout.

### 0.1 The core must never import the LLM package

The README's *"nothing here talks to a network"* is the load-bearing promise —
it is what lets FillerAI run inside the environment where the real form lives.
"Optional dependency" is not enough, because an import at module scope in the
wrong place makes it mandatory in practice.

So: one new top-level package, `fillerai/llm/`, which **nothing under
`fillerai/` imports at module scope.** The two places that use it import it
inside the function that needs it, after checking it is configured:

```python
def cmd_propose_rules(args):
    from ..llm import rules          # not at module scope, deliberately
    ...
```

And a test enforces it rather than a convention:

```python
class TestFence(unittest.TestCase):
    def test_core_never_imports_llm(self):
        for mod in ('fillerai', 'fillerai.cli', 'fillerai.infer',
                    'fillerai.train', 'fillerai.web.server', ...):
            importlib.import_module(mod)
        self.assertNotIn('fillerai.llm', sys.modules)
```

`fillerai.llm` goes in `pyproject.toml`'s `packages` list so it ships, and
`dependencies` stays `[]`.

### 0.2 The network has to be one function, so tests can replace it

This is the whole testing strategy and it is worth getting right before
writing a feature.

```
fillerai/llm/
    transport.py   the ONLY module that opens a socket
    client.py      builds requests and reads responses; no network
    prompts.py     prompt text and the JSON schemas for structured output
    rules.py       phase 1
    typing.py      phase 2
    cost.py        token estimates and the spend ceiling
```

`transport.py` defines one protocol with one method:

```python
class Transport:
    def send(self, request: dict[str, Any]) -> dict[str, Any]: ...
```

and three implementations:

| | what it is | when |
| --- | --- | --- |
| `UrllibTransport` | `urllib.request` + `json`, ~40 lines with retry and backoff | the default; keeps `dependencies = []` true |
| `SdkTransport`, `OpenAiSdkTransport` | the `anthropic` or `openai` SDK, used automatically if it imports *and* matches the provider | when it is installed, and required for phase 3 |
| `RecordedTransport` | replays a JSON fixture, raises on a request it has no recording for | every test |

**On stdlib versus the SDK.** The Messages API is HTTPS and JSON, so
`urllib.request` genuinely calls it. What that gives up is streaming, typed
errors, and hand-maintained retry — and for phases 1 and 2 none of those
matter, because each is a single non-streaming call made offline by a person
at a terminal. Prompt caching is a request field, not a client feature, so it
works either way. Phase 3 makes thousands of calls and should require the SDK;
that is a reasonable thing for an optional, explicitly-enabled engine to
demand, and it is stated as a precondition in section 3.

### 0.3 Configuration

Following the existing convention (`FILLERAI_ADMIN_PASSWORD`,
`FILLERAI_HOME`, `FILLERAI_DATABASE_URL`):

| variable | meaning |
| --- | --- |
| `FILLERAI_LLM_KEY` | the API key, whichever provider is in use |
| `FILLERAI_LLM_PROVIDER` | `anthropic` or `openai` |
| `FILLERAI_LLM_MODEL` | overrides the per-task default |
| `FILLERAI_LLM_BASE_URL` | for Bedrock, Vertex, Foundry, Azure or a gateway |

Failing all of those, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are read, and
whichever one is set also says which provider was meant.

Defaults per task, chosen for the job rather than uniformly: `claude-opus-5`
or `gpt-5` for rule proposal, which is a reasoning task done once;
`claude-sonnet-5` or `gpt-5-mini` for semantic typing, which is 54 easy
classifications.

### 0.4 Two providers, one question

*Added after the rest of this plan was written, and built the same day as
phases 0 and 1.*

The plan above assumed one service. Supporting a second turned out to cost a
module rather than a rewrite, because the seam was already in the right place:
`fillerai/llm/providers.py` holds the key variable, the path, the request
shape and the reply shape, and nothing above it changed. The prompt, the
validation gate and the three commands are the same code for both, and
`tests/test_llm_providers.py` runs the same proposed rules through both
readers and asserts the gate reaches identical verdicts.

Three differences were worth handling rather than papering over:

- **The token limit has two names.** `max_tokens` is refused outright by
  OpenAI's reasoning models, so the OpenAI request sends
  `max_completion_tokens`.
- **Only one side enforces a schema for free.** OpenAI's strict mode
  guarantees the shape, but only over a subset of JSON Schema that forbids the
  open-ended map `RULES_OUTPUT_SCHEMA` needs for `when`. So `strict_ready`
  decides per schema, the schema is sent either way, and nothing downstream
  relies on the guarantee — a proposed rule goes through the gate whoever
  wrote it.
- **A refusal and a truncation still arrive as a successful call**, just in
  different fields: `stop_reason` against `content`, or `finish_reason`
  against `choices[0].message.refusal`. Both are checked before anything tries
  to parse the text.

The provider is inferred rather than demanded — an explicit flag, then
`FILLERAI_LLM_PROVIDER`, then a model name that gives itself away, then which
key variable is set, then what the neutral key begins with — and `llm status`
prints *which* of those decided it, because a guess that cannot explain itself
is worse than a prompt.

The key is read from the environment, never written to the library, never
into the SQLite store, never logged, and never echoed back by any command. A
`fillerai llm status` reports whether a key is present, which model each task
would use, and which transport is active — without printing the key.

---

## 1. Phase 1 — proposing the generation rules

The highest-leverage piece, and the one to build first.

### What it does

```bash
# Read a schema, propose the form's business rules, write them out.
fillerai propose-rules schema.json -o rules.json

# What it would cost, without spending it.
fillerai propose-rules schema.json --dry-run

# Merge reviewed rules back into a field spec.
fillerai apply-rules examples/claims_intake.fields.json rules.json -o merged.json
```

Two commands, not one, and that is the important decision: **the model's
output is never written into a spec automatically.** It lands in a separate
file a person reads.

### The prompt

- **System**, cached: the `Derived` contract, lifted from
  `schema.py`'s docstring rather than paraphrased, plus the `follows` / `when`
  / `otherwise` spelling from `extract/spec.py`, plus four worked examples
  taken from the 27 rules already in `examples/auto_insurance_quote.fields.json`.
  Roughly 1,500 tokens, identical across runs, so it caches.
- **User**: the schema rendered compactly — `name | label | semantic_type |
  control | options` — which measured 9,272 characters for a 54-field form.

No records are sent. That is what makes this phase safe to ship first: the
input is field names and option lists, which the README already notes are *"in
the business manual, not in anybody's submitted record"*.

### The output contract

Structured output via raw JSON schema, not Pydantic, because there are no
dependencies:

```python
OUTPUT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field":     {"type": "string"},
                        "follows":   {"type": "array", "items": {"type": "string"}},
                        "when":      {"type": "object", "additionalProperties": True},
                        "otherwise": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number"},
                        "why":       {"type": "string"},
                    },
                    "required": ["field", "follows", "when", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rules"],
        "additionalProperties": False,
    },
}
```

`why` is not decoration. A rule a person cannot audit is worse than no rule,
and it is the line the reviewer actually reads.

### The validation gate — the part that matters

The model's output is a **proposal**, and every proposal runs the gauntlet of
machinery that already exists before a human ever sees it:

1. `spec._derived_from_spec` parses it, or it is dropped.
2. Every name in `follows` is a real field on this form, and not the target
   itself.
3. Every value in `when` and `otherwise` is in the target's own option list —
   because `_as_written` deliberately does *not* fall back to
   `choose_option`, so a value the form cannot hold is a reported error rather
   than silent noise.
4. `dataset._resolution_order`'s DFS accepts the whole set without a cycle.
5. **Generate 200 records with the rules applied and run `validate` and
   `coherence_report`.** A rule that makes the data incoherent is dropped.

Each dropped rule is reported with the check that killed it. The report is in
the `inspect` house style, with `?` on anything below 0.7:

```
  == proposed rules: 19 kept, 4 dropped ==
    premium_band        follows coverage_tier, vehicle_use    0.91
      a premium band is set by coverage and how the car is used
  ? garage_type         follows garaging_postal_code          0.58
      city ZIPs more often mean street parking
    -- dropped --
    vin_prefix          follows vehicle_make   values not in the field's options
    coverage_tier       follows premium_band   cycle: premium_band -> coverage_tier
```

### Acceptance test

This phase has a falsifiable success condition, which is the reason to build
it first. `claims_intake.html` currently declares no rules and saves **5.0%**
of the work. Run `propose-rules` on it, apply the surviving rules, regenerate,
retrain, re-simulate.

**Ship it if the saving on `claims_intake` rises meaningfully above 5.0%
without `validate` or `coherence_report` reporting a single new problem.**
If it does not, the feature does not work and phase 2 is the only thing worth
keeping.

The honest caveat, which belongs in the command's own output: a model
proposing rules is **inventing plausible business logic, not observing a real
one**. It gets `vehicle_model → vehicle_make` right because that is a fact
about the world; it will guess at a carrier's coverage ladder. The generated
data is exactly as honest as the rules, which is why `apply-rules` is a
separate step with a human in the middle.

### Cost

~2,300 tokens in, ~2,000 out. About **$0.06 on Opus 5**, once per form design.

---

## 2. Phase 2 — semantic typing as a disagreement report

Smaller, safer, and it fixes a defect that exists today.

### The defect

`infer.py` matches regexes against a field's *name* before considering what
the field is. On `auto_insurance_quote`:

| field | inferred | confidence | matched |
| --- | --- | ---: | --- |
| `telematics_eligible` | `phone` | 0.80 | `/tel/` |
| `prior_claims` | `claim_number` | 0.80 | `/claim/` |
| `claim_free_discount` | `claim_number` | 0.80 | `/claim/` |
| `business_use_rider` | `company` | 0.74 | `/business/` |
| `lienholder_required` | `policy_number` | 0.74 | `/policy/` |

All five are yes/no `<select>`s, and all five are **above 0.70**, so `inspect`
prints no `?` and nobody looks. A wrong `semantic_type` is not cosmetic:
generation is driven entirely by it, so `telematics_eligible` typed as `phone`
renders phone numbers into a Yes/No column.

### What it does

```bash
fillerai infer-check examples/auto_insurance_quote.fields.json
```

Run the model over the schema, keep the ladder's answer as authoritative, and
print **only the disagreements** — both readings, the ladder's `evidence`
string, and the model's reason:

```
  5 of 54 fields read differently

    telematics_eligible   rules: phone 0.80   model: boolean
      rules: name matches /(phone|tel|fax|contact_?number)/
      model: label "Telematics Eligible", options Yes / No

    business_use_rider    rules: company 0.74   model: boolean
      ...

  to accept the model's reading, set semantic_type in the spec — an
  explicit semantic_type is never overruled by inference
```

The last line matters: `spec.load` already treats a stated `semantic_type` as
given, so the fix path exists and needs no new mechanism. The command changes
nothing; it tells a person what to change.

A `--strict` flag exits non-zero when there are disagreements, so it can gate
a build. (Noting that there is no CI in this repository at all — no
`.github/workflows` — so today that means a pre-commit habit, not a pipeline.)

### Acceptance test

Ground truth already exists, which makes this gradeable rather than
impressionistic:

- **Recall**: it must flag all five fields in the table above.
- **Precision**: on `claims_intake.html`, where all 45 fields are inferred
  above 0.70 and the ladder is right, it must raise **no more than two**
  disagreements. A report that cries wolf on a form the rules already handle is
  worse than no report, because nobody will run it twice.

Both forms are in the repository, so this eval costs two API calls to run.

### Cost

Two calls per form, ~2,300 tokens in and ~800 out. Under a cent on Sonnet 5.

---

## 3. Phase 3 — the `llm` engine, to settle the prediction question

**Precondition: phases 1 and 2 are shipped, and someone still wants the
question answered.** This is not a feature, it is an experiment with a
predicted outcome, and the plan says so in advance so that a negative result
reads as the answer rather than a failure.

### Why the architecture makes it cheap to do honestly

An engine is one file in `fillerai/train/algos/` implementing `Engine` and
calling `register()`, plus one line in `algos/__init__.py`. A `Guess` carries a
raw `score` — explicitly *"a probability under that engine's own assumptions,
which are wrong in their own particular way"* — and the shared isotonic
calibration turns it into the number a user reads. That is the machinery that
keeps naive Bayes usable, and it would do the same job here: **the model's
confidence becomes measured rather than asserted**, on the same held-out
records, against the same curve. Then `train --compare` puts it beside the
other six and the argument is over.

### The three real engineering problems

**What does `to_dict` save?** Every engine must serialise and reload — that is
how the library keeps a model and how `predict` reopens one. An LLM engine has
no weights. It saves the model id, the prompt template hash, the compact
schema it was fitted against, and **a cache of every answer it has given**,
keyed by `(target, frozenset(known.items()))`. That cache is the engine: it is
what makes a reloaded model reproducible, and it is what stops calibration
costing a fortune.

**Calibration, not fitting, is the expense.** Calibration predicts every field
of every held-out record at five evidence sizes. That is why `forest`'s ten
minutes at 20,000 records turned out to be 9.5 minutes of calibration and 12.7
seconds of fitting. At 5,000 held-out records an LLM engine would make tens of
thousands of calls. Three mitigations, all required, not optional:

- a new `TrainOptions.tuning` knob capping the calibration holdout for this
  engine specifically (the `knobs` field on `Algorithm` already exists for
  exactly this kind of per-algorithm setting);
- the answer cache above, which collapses repeated `(target, evidence)` pairs
  and is a large fraction of them;
- route calibration through the Batch API, which is half price and has no
  latency requirement because nobody is watching.

**A hard spend ceiling, enforced before the first call.** `cost.py` estimates
the run from the holdout size and the evidence sweep, prints it, and refuses
to start above `--max-spend`. A training run that quietly costs $400 is the
failure mode that would end the experiment permanently.

### Expected result

It loses on generated data. Section 1 of the analysis says the ceiling is
information, not intelligence, and
[weight-based-training.md](weight-based-training.md) says invented records
carry only the relationships the generator put in them. A result of *"the
`llm` engine scored no better than `statistical`"* is the expected outcome and
says almost nothing about real history — which is the point of writing the
prediction down first.

---

## 4. Phase 4 — free text

Sketched, not planned, because it should not start until there is real data to
point it at.

This is the one place a model adds a capability rather than a percentage. A
field with more than 500 distinct values is classed `open`, and an `open`
field is never a prediction target for any engine — `train/model.py:317` and
`:494`. On the realistic forms that is 22 of 45 fields on `claims_intake` and
11 of 21 on `patient_registration`, though on generated data most of those are
identifiers nothing could predict.

Two things make it a different shape of work from phases 1–3, and both need
deciding before any code:

- **The confidence story does not transfer.** The isotonic curve maps a score
  to *how often an answer at that score was right*, and a paragraph is not
  right or wrong. So a drafted narrative cannot be a `filled` cell in
  `simulate`; it has to be a distinct source, with its own effort accounting —
  reading and editing a draft is not `seconds_to_review`.
- **It is a UI change as much as a model change.** The value is a draft the
  agent edits, not a value they accept, which is a different interaction from
  every other cell on the form.

---

## 5. Testing a non-deterministic component in a deterministic suite

The suite is 537 tests, offline, and every one of them is reproducible. That
must not change.

**Fixtures, recorded once.** `RecordedTransport` replays
`tests/fixtures/llm/*.json`, one file per scenario, each holding the request
and the response it got. A `--record` flag on a developer script re-records
them against the real API when a prompt changes; the recorded file is then
committed and reviewed like any other test data. `RecordedTransport` raises on
a request it has no recording for, so a changed prompt fails loudly instead of
silently going to the network.

**Every LLM test runs offline.** `test_llm_rules.py`, `test_llm_typing.py` and
`test_llm_fence.py` join the existing suite and need no key. What they test is
the part that carries the risk anyway: the validation gate in 1.4, which is
ordinary deterministic code that takes a fixed JSON blob and decides what to
keep.

**Live tests are separate and opt-in.** They go in `livetests/` at the
repository root rather than under `tests/`, so `unittest discover -s tests`
never reaches them, and they skip themselves unless `FILLERAI_LLM_LIVE=1`.
That directory holds the two acceptance evals from sections 1 and 2. They cost
money, so they run when someone decides to spend it.

**The fence test** from 0.1 is the one that must never be deleted.

---

## 6. Order, gates, and what would stop this

| | build | gate before the next |
| --- | --- | --- |
| **0** | `llm/` package, three transports, config, `llm status`, fence test | fence test passes; `dependencies` still `[]` |
| **1** | `propose-rules`, `apply-rules`, the validation gate | `claims_intake` rises above 5.0% with no new coherence problems |
| **2** | `infer-check` | flags all five known-wrong fields; ≤2 false alarms on `claims_intake` |
| **3** | the `llm` engine | only if someone still wants the question settled |
| **4** | free text | only once there is real submission history |

Phase 0 is a day's work and is almost all testing scaffolding. Phases 1 and 2
are each a module and a command. Phase 3 is larger than it looks, because the
calibration cost is a design problem rather than a knob.

**What should stop this plan.** If phase 1's acceptance test fails — proposed
rules do not raise `claims_intake` above 5.0%, or they raise it only by
inventing business logic a reviewer rejects — then the LLM's value in this
project is phase 2 alone, which is a one-command diagnostic rather than a
feature, and the rest should not be built.

---

## 7. What is deliberately not in this plan

From the analysis, restated so the plan does not drift into them later:

- **Generating records with a model.** 20,000 records take 7.6 seconds and
  cost nothing today; the same rows are $37–185, hours of wall clock, and not
  reproducible from a seed. Rules from a model, rows from the generator.
- **Fill-time autofill as a product feature.** Three separate reasons, any one
  of which is sufficient: there is almost no headroom (89% of what gets left
  blank is closed to any model); ~1,000 output tokens is 10–20 seconds against
  a 45-second total saving and a 1.2-second per-field budget; and the typed
  fields are live customer PII, which is a strictly larger ask than sharing
  the training data this project exists to avoid sharing.
- **Anything that makes an existing command need a key.** `extract`,
  `generate`, `train` and `simulate` keep working exactly as they do now, with
  no network, forever. That is the promise the fence test protects.
