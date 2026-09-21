# Weight-based training: what it would take, and what it would buy

FillerAI's five engines learn by counting, by partitioning, or by keeping
records. None of them learns a weight. This document works out what adding a
weight-based engine would mean: how training would run, how inference would
run, what each candidate costs under this project's constraints, and which
ones are worth building.

**Both of its recommendations were then built**, so section 0 records what the
engine and the vote weights actually do now, and where the estimates below
turned out to be wrong. Everything from section 1 on is the analysis as it was
written, which is the honest way round: it was the reasoning that chose the
work, and its mistakes are worth keeping visible.

Everything with a number attached was measured on `main` (475 tests passing)
against the claims intake form and 20,000 generated records — the same setup
as [**Training, serving, and what happens at 20,000
records**](training-and-scale.md), so the costs here sit beside that
document's rather than replacing them. The scripts are in section 9.

---

## 0. What was built, and where this document was wrong

Two things, in the order section 8 recommended.

**Learned vote weights** (`fillerai/train/algos/combine.py`), no new engine.
Five features per vote - the weight the engine proposed, its strength, how many
records back it, how decisive it is, and whether it is the marginal floor -
and six parameters fitted to predict whether that voter was right. Fitted on a
slice taken off the front of the holdout, never on the rows that calibrate, and
kept only if it beats the hand-picked arithmetic on records neither of them
saw. Otherwise the run says so and the old arithmetic stays.

**A `linear` engine** (`fillerai/train/algos/linear.py`). Per-target softmax
over `field=value` tokens, with the fields too wide to enumerate hashed into
buckets - which is the whole point, per section 4.

Four things this document got wrong, all of them worth reading before trusting
the rest of it.

**The weight that a weighted average needs is not a probability.** Section 3.1
says to fit a logistic regression and use it; the first implementation did
exactly that and changed nothing measurable. A logistic model on features this
strong saturates: two voters that deserve to be three times apart both come out
at 0.98, and the ballot normalises the difference away. Using the *odds* -
`exp` of the same linear score - instead of the probability turned a 0.08%
improvement in log loss into 3-5%.

**Vote weights cannot touch a ballot with one voter at all,** because
`Ballot.result` divides by the weight cast. On the generated claims form 95% of
ballots have exactly one voter, so the learned weights are nearly inert there -
a few tenths of a percent - and the honest gate is log loss rather than top-1
accuracy, which is mostly blind to what better weights change. On a form where
two predictors genuinely compete the same code cuts log loss by 3-5%. Same
story as everywhere else in this project: invented records only carry the
relationships the generator put in them.

**A shortlist is not optional for a linear engine, it is load-bearing.** Given
every field, the fit will happily learn confident weights for a column of pure
noise, and because the confidence curve is shared across all fields, one
overconfident field drags every field's confidence down. With the noise columns
in, a perfectly determined answer came back at 0.54. The fix is the shortlist
`bayes` and `nearest` already use (`relevance`), applied to the bucketed tokens
so a wide field is judged as it will be used.

**Hashing needs a ceiling, and section 3.3's size estimate missed it by 14x.**
That estimate assumed only enumerable sources. Nine bucketed fields at 4,096
buckets each, against a target with 194 candidate values, produced a **102 MB**
model on the claims form. The fix is a ceiling on the *numbers* rather than the
rows - 40,000 per field, keeping the largest - which brought it to 6.5 MB and
doubles as feature selection, since a bucket that learned nothing is the first
thing dropped. Counting numbers rather than rows is what protects the case the
engine exists for: thousands of buckets against a five-answer field is cheap,
and that is exactly city to state.

### What it measures, on the same 20,000 records as everything below

| | estimated in section 3.3 | measured |
|---|---|---|
| engine fit | 4.7 min (10 passes) | **144.5 s** |
| engine JSON | 7.2 MB | **6.52 MB** |
| engine load | 119 ms | 240 ms |
| whole-form prediction | 0.17 ms | **0.96 ms** |
| calibration | ~3 s | 14.3 s |

The fit came in faster than estimated because the shortlist cut the fields
offered to each target from 31 to at most 10. Prediction came in slower because
0.17 ms was measured on a bare weight lookup; 0.96 ms is the real engine,
through the ballot, the marginal floor and `predict_field`, over all 44 fields.
It is still the second fastest engine in the project, behind `statistical`'s
0.50 ms and ahead of `tree`'s 1.50 ms. A whole `linear` run at 20,000 records
is about three minutes against `statistical`'s 51 seconds, and nearly all of
the difference is the one stage a live log can narrate.

On the claims form it bucketed nine fields and fitted `home_state` from
`home_city` **and `home_postal_code`** at strength 1.00 - and
`home_postal_code` has 4,458 distinct values, so the conditional tables cannot
see it in either direction. That is the hole in the other five engines, closed,
on a real form rather than a constructed one.

### What was not built

Boosting (section 6) and the three declined candidates (section 7) were not
built, for the reasons given there. Section 6's reason got stronger, not
weaker: `forest`'s cost really is its inference cost, and section 2 has the
measurement.

---

## 1. What "weight-based" has to mean here

A weight-based method is one whose fitted parameters are real numbers adjusted
against a loss, rather than counts read out of a table (`statistical`,
`bayes`), a partition of the records (`tree`, `forest`), or the records
themselves (`nearest`). That difference is the whole of the appeal: a weight
can be *negative*, it can be *small*, and it is fitted knowing about the other
weights, which is exactly the thing a conditional table cannot do.

Three properties of this problem decide whether any of that survives contact
with a form. They are stated in
[`algos/base.py`](../fillerai/train/algos/base.py) as the reason the textbook
implementations are ruled out, and each one is a demand on a candidate:

**D1 — Features and targets swap roles per record.** There is no fixed feature
vector and no single target. An engine is fitted per predictable field and
asked about every other one, so every cost below multiplies by the number of
targets. Measured on the claims form at 15,000 records: 44 writable fields, of
which **22 are usable** (`constant` or `enumerable`) and 22 are `open` and
predicted by nobody.

**D2 — Almost every feature is missing at predict time.** The agent has typed
one to eight boxes out of 44; the calibration sweep encodes exactly that
(`_EVIDENCE_SIZES = (1, 2, 3, 5, 8)`,
[`model.py:569`](../fillerai/train/model.py)). A model fitted with all features
present and then asked with one present is being asked a question it never
saw. This is the demand that does the most damage below, and section 3.6 is
about it.

**D3 — The confidence has to be calibrated, so the model can decline.** An
engine returns a `Guess` carrying a `distribution`; the shared layer mixes in
the marginal floor (`Ballot.floor`,
[`base.py:327`](../fillerai/train/algos/base.py)) and maps the raw score
through an isotonic curve measured on held-out records
(`_isotonic`, [`model.py:194`](../fillerai/train/model.py)). A method that
cannot produce a distribution over candidates loses the floor, the
alternatives list, and most of what calibration has to work with.

And over all three sits the standing constraint: **Python 3, standard library
only.** No numpy. Every inner product is a Python `for` loop over lists of
floats. That constraint decides most of what follows, so section 2 measures it
before anything is argued from it.

One thing is cheap, and worth saying early. Structurally a new engine really
is one file: `fillerai/train/algos/<name>.py` implementing `Engine` and
calling `register(Algorithm(...))`, plus one line in
[`algos/__init__.py`](../fillerai/train/algos/__init__.py). Nothing outside
that package names an algorithm except prose and the 1.x model migration in
[`model.py:409`](../fillerai/train/model.py). The UI already renders whatever
`Algorithm.knobs` and `Engine.summary()` return. So the cost of a candidate is
its mathematics and its fit to D1–D3, not plumbing.

---

## 2. The measurement that decides it: pure-Python SGD throughput

Everything below rests on how fast stochastic gradient descent runs with no
numeric library underneath it, so that was measured first rather than guessed.

The loop measured is the honest one, not a naive one. Features are sparse —
one token per filled field, `field=value` — so a step touches only the weight
rows of the fields actually present, and the gradient is applied only to those
rows. Each record is presented once per evidence mask, mirroring
`_EVIDENCE_SIZES`, because of D2. Per step the cost is therefore
`2 × K × F_active` float operations, where `K` is the number of candidate
values for the target and `F_active` the number of fields typed.

One epoch over 1,500 records, all 22 usable targets, five masks per record:

| target | candidate values `K` | µs per step |
|---|---|---|
| `police_report_filed` | 2 | 4.8 |
| `home_state` | 8 | 7.4 |
| `home_city` | 23 | 13.3 |
| `incident_time` | 44 | 22.9 |
| `mailing_city` | 78 | 36.3 |
| `deductible_amount` | 101 | 44.8 |
| `mailing_postal_code` | 194 | 81.7 |

Cost is linear in `K`, as expected, and the wide fields dominate. Summed over
all 22 targets: **2.8 s per epoch over 1,500 records**, which scales to
**28 s per epoch over the 15,000 records** a 20,000-record run learns from.

Where that lands. The engine-fit column and the engine-JSON column were
measured here, by timing `Algorithm.fit` on the same 15,000 records and
serialising what came back; the whole-run, load and predict columns are the
published figures from
[training-and-scale.md](training-and-scale.md#cost-per-algorithm-at-20000-records):

| engine | engine fit | engine JSON | whole run | load | predict |
|---|---|---|---|---|---|
| `nearest` | 0.5 s | 0.14 MB | 82 s | 4 ms | 4.35 ms |
| `tree` | 3.0 s | 0.16 MB | 69 s | 19 ms | 1.50 ms |
| `bayes` | 5.0 s | 3.2 MB | 64 s | 82 ms | 1.69 ms |
| `statistical` | 8.5 s | 0.03 MB | 51 s | 3 ms | 0.50 ms |
| `forest` | 12.7 s | 3.5 MB | 10.4 min | 639 ms | 19.8 ms |
| **softmax, 5 epochs** | **2.3 min** | **7.2 MB** | — | 119 ms | 0.17 ms |
| **softmax, 10 epochs** | **4.7 min** | **7.2 MB** | — | 119 ms | 0.17 ms |

The first surprise in that table is not the softmax row, it is how small a
share of a run the engine is. Timing each stage separately on the same split:

| stage | `statistical` | `forest` |
|---|---|---|
| profile the columns | 0.7 s | 0.7 s |
| verify the rules | 21.9 s | 21.9 s |
| fit the engine | 8.5 s | 12.7 s |
| **calibrate** | **8.7 s** | **9.5 min** |
| whole run, as published | 51 s | 10.4 min |

So the thing that makes `forest` a ten-minute algorithm is not its trees. It is
calibration, which predicts every field of every held-out record at five
evidence sizes ([`model.py:572`](../fillerai/train/model.py)) and therefore pays
the engine's inference cost 5,000 records over. `forest` answers in 19.8 ms
where `statistical` answers in 0.50 ms, and 567.8 s against 8.7 s is almost
exactly that ratio. **Calibration cost tracks an engine's inference cost, not
its fit cost.**

That is worth writing down on its own, because it revises what
[training-and-scale.md §5](training-and-scale.md#forest-at-scale) says to do
about `forest`: capping the per-tree record sample would trim the 12.7 s and
leave the 9.5 minutes where they are. (The remaining ~11 s of the published
51 s is the CLI reading a 34 MB dataset off disk and writing the model back,
which no engine choice affects.)

And it cuts the good way for a weight-based engine, which is the part nobody
would guess. Its fit would be the most expensive here by an order of magnitude
— but at 0.17 ms per form it answers faster than the conditional tables do, so
its calibration would be the *cheapest of any engine*, about 3 s. A ten-epoch
linear run comes out at roughly 22 s of rules, under 5 minutes of fitting and
3 s of calibration: call it five minutes against today's 51 s, with nearly all
of the difference in one stage a live log can report honestly.

**One caveat, and it is the important one: throughput was measured,
convergence was not.** How many epochs a form actually needs is the first
thing a prototype has to settle, and it is the difference between a two-minute
algorithm and a twenty-minute one. Ten epochs is a placeholder, chosen because
it is what sparse one-hot problems of this size usually want, not because
anything here measured it.

Even at the pessimistic end this is not disqualifying: `forest` already costs
10.4 minutes and ships. It does mean a weight-based engine belongs on the slow
side of the picker, and that the training UI's live log
(`api_train_start`, [`web/server.py:643`](../fillerai/web/server.py)) stops
being a nicety and becomes the only thing making the run watchable.

---

## 3. Candidate A — per-target softmax regression

The core candidate. One multinomial logistic regression per predictable
field, over one-hot indicators of the other fields' values.

### 3.1 How training would work

1. **Candidates.** For target `t`, the classes are `profiles[t].values` — the
   distinct values the shared layer already counted, capped at `MAX_DISTINCT`.
   `K` is their number.
2. **Features.** One token per filled source field, `"<field>=<value>"`. No
   numeric encoding, no scaling: every field on a form is categorical by the
   time `features.normalise` has run.
3. **Weights.** A dict from feature token to a list of `K` floats, plus a `K`
   bias. Sparse in the only way that matters — a token that never appears has
   no row.
4. **Masked presentation.** Each record is presented several times, each time
   with only a random subset of its filled fields visible, sizes drawn from
   `_EVIDENCE_SIZES`. This is not a refinement; without it the model only ever
   sees full rows and D2 guarantees it is asked something else.
5. **Loss and step.** Cross-entropy; softmax over the summed logits; the
   gradient applied to the bias and to the active rows only. L2 shrinkage
   toward zero is what stops a value seen twice from earning a large weight —
   it plays the role that leave-one-out and the permutation test play for
   lambda in [`associate.py`](../fillerai/train/associate.py).
6. **Stopping.** Early stopping on a sub-holdout carved out of
   `context.records`. This is already the idiom here — `tree` grows on a
   subset and scores on the rest (`_score`,
   [`tree.py:512`](../fillerai/train/algos/tree.py)), `nearest` measures on
   rows it does not hold (`_measure`,
   [`nearest.py:224`](../fillerai/train/algos/nearest.py)) — so nothing in the
   shared layer has to change. The run's real holdout stays untouched for
   calibration, which is the point of it.
7. **Strength.** Per target, the share of guessing errors the fitted model
   removes over always answering the commonest value, measured on that
   sub-holdout. That is the same quantity `tree._score` and `associate`
   report, which is what lets the field report mix engines.

### 3.2 How inference would work

Cheap and fixed-cost. For target `t` and the typed fields `known`: start from
the bias, add the weight row of each present `field=value` token that exists,
softmax, and hand the resulting distribution to a `Ballot` with the target's
strength as its weight, then `ballot.floor(profile)` as every engine does.
`used_evidence` is true when at least one token was found, which is what makes
the difference between `basis="learned"` and `basis="usual"` downstream
([`model.py:314`](../fillerai/train/model.py)).

`sources(name)` comes from ranking that target's fields by the largest
absolute weight any of their values carries; `reach(name, available)` from the
share of that total lying in fields the agent could have typed. Both are
proxies rather than measurements, which is a real step down from `statistical`,
where `sources` is the list of predictors that survived a permutation test.
`reach` feeds `suggest_seed_fields`
([`evaluate.py:103`](../fillerai/train/evaluate.py)), so a sloppy proxy there
shows up as bad advice about which boxes to type first.

### 3.3 What it costs, measured

A weight set of exactly the right shape for the claims form — every usable
source value against every target's candidates — is:

| | |
|---|---|
| weight rows | 19,740 |
| floats | 795,424 |
| engine JSON | **7.2 MB** |
| engine JSON, pruned at \|w\| > 1 and two decimals | 5.5 MB |
| `json.loads` | 119 ms |
| whole-form prediction from 5 typed fields | **0.17 ms** |

Two things worth noticing. It is the largest engine in the project — 7.2 MB
against 3.5 MB for `forest`, 3.2 MB for `bayes` and 0.03 MB for `statistical`
— and pruning small weights only recovers a quarter of it, because a dense
weight matrix is dense. And prediction is *fast*, faster than the conditional
tables, because answering is a dict lookup and `K` additions with no bucket to
scan. The headline property from
[training-and-scale.md](training-and-scale.md#inference-does-not-grow-with-the-data)
survives: the model is still a summary, so inference is still independent of
how many records trained it.

(The 0.17 ms excludes the `Ballot` and calibration wrapper that
`predict_field` adds; `statistical`'s published 0.50 ms includes it. Read the
two as the same order, not as a 3× win.)

### 3.4 What it buys: interactions, sometimes

The frozen test that justifies the trees
([`tests/test_algos.py:37`](../tests/test_algos.py)) is a dataset whose only
signal is a pair: `relationship` is fixed by `policy` **and** `role` together,
while either alone leaves it near a coin flip. Measured on this branch,
`statistical` falls back to the marginal and answers `self` at 0.48 confidence;
`tree` answers `child` at 0.95 and `forest` at 1.00.

Run against that same data, a softmax over single-field one-hots:

| | both fields typed | `policy` alone (truth: a coin flip) |
|---|---|---|
| `statistical` | falls back to the marginal — `self` at 0.48 | marginal |
| `tree` | correct at 0.95, `forest` at 1.00 | — |
| softmax, single-field weights | **all four cells correct at 1.00** | `child` at 0.75 |
| softmax, plus pair cross-features | all four correct at 1.00 | `self` at 0.52 |

So it passes, and that is a genuine result: an additive weight model handles
this interaction where the vote of conditional tables cannot. But the reason
is narrower than it looks, and the same experiment with the same four cells
collapsed to two classes — a true XOR — shows it:

| two-class version of the same data | both fields typed |
|---|---|
| softmax, single-field weights | **two of four cells wrong** |
| softmax, plus pair cross-features | all four correct |

The three-class layout happens to be linearly separable: the diagonal answer
wins wherever neither specialist gets both of its boosts. Collapse it to two
classes and there is no such room, and a linear model is a linear model. A tree
does not care which case it is given; it splits, and both are easy.

The fix is explicit cross-features, a token per co-occurring *pair* of field
values. On the three-field toy that took the model from 12 weight rows to 48.
On the claims form the token space stops being one per field value — 940 of
them, section 3.3 — and becomes one per co-occurring pair of values across 231
field pairs, bounded only by what the data happens to contain. That is a large
multiple of a model file already the biggest in the project, for interactions
that `tree` captures in a 3-second fit.

**So interactions are not the argument for this engine.** If interactions are
what is wanted, `tree` and `forest` exist and are cheaper at it.

### 3.5 What it loses: the explanation, and this part is a real cost

`statistical` answers with *"home_city = Austin → TX in 100% of 12 records"*.
That sentence is not a summary of the reason, it is the reason, and the field
report and the simulator both lean on it: an agent decides whether to accept a
value by reading it.

A weight model's honest sentence is *"the strongest contributions were
home_city and mailing_postal_code"*. Nothing in it can be checked against the
data by hand, the weights are only meaningful relative to each other, and a
negative weight — the model's real advantage — has no plain-language form at
all. `bayes` already sits at this end of the scale, and the project keeps it
partly to demonstrate what calibration rescues; a second engine there is a
cost, not a novelty.

### 3.6 The structural problem: one weight set, every evidence size

This is the finding worth the most attention, because it is not in any
textbook's list of logistic regression's drawbacks — it comes from D2.

The existing engines are all *additive over whatever is present, then
renormalised*. `Ballot.cast` accumulates `weight × probability` and
`Ballot.result` divides by the weight actually cast
([`base.py:316`](../fillerai/train/algos/base.py)). So removing a predictor
removes its vote and changes nothing else: each predictor carries its own
table, its own lambda, its own bucket support
(`link.strength * support_weight(rows)`,
[`statistical.py:57`](../fillerai/train/algos/statistical.py)). Answering from
one typed field and answering from eight are the same computation over
different numbers of voters.

A softmax has **one** weight per `field=value`, shared across every evidence
size. The value that makes the pair `(policy, role)` come out right is not the
value that makes `policy` alone come out right, and one number has to serve
both. Measured on the interaction data, where the truth from `policy` alone is
a 50/50 coin flip:

| | answer from `policy` alone |
|---|---|
| softmax, trained on full rows | `child` at 0.85 |
| softmax, trained with masked subsets | `child` at 0.75 |
| softmax with cross-features, full rows | `self` at 0.52 |
| softmax with cross-features, masked | `child` at 0.79 |

Masking moves it in the right direction without fixing it. Cross-features on
full rows land almost exactly on the honest answer, for a reason worth
noticing — the pair token absorbs the joint signal, which frees the
single-field weights to report the marginal. Then adding masking on top breaks
it again. Not one of the four combinations is reliably honest here, and that is
the finding: one number is being asked to answer several different questions,
and which question it ends up serving depends on the training recipe rather
than on anything a user can see.

The shared calibration is a partial rescue only. It is indexed by raw score
alone and pools every evidence size into the same ten bins
(`_calibrate`, [`model.py:572`](../fillerai/train/model.py)). For the existing
engines that is sound, because a thin-evidence answer already *has* a low raw
score — the ballot weight says so. For a softmax that is confidently wrong on
thin evidence and confidently right on thick, the two land in the same top bin
and the curve reports their average. The number a user reads stays honest on
average and is wrong in both directions case by case, which is precisely the
failure mode this project built the calibration layer to avoid.

Making it right would mean either a weight set per evidence size — the model
file multiplied by five — or a calibration curve indexed by how many fields
were typed, which is a change to the shared layer and therefore to every
engine's numbers. Neither is small.

---

## 4. Candidate B — the same weights, over hashed features

This is the candidate with a real argument behind it, and it is not accuracy.

[training-and-scale.md §5](training-and-scale.md#the-distinct-value-ceiling)
records the one defect that matters before real data: a field with more than
`MAX_DISTINCT` (500) distinct values is classed `open`
([`features.py:29`](../fillerai/train/features.py)), and `learn_links` admits
only `enumerable` and `constant` fields as target **and as source**, so an
`open` field is invisible in both directions. On a two-column set where city
determines state perfectly, the model answers `state` at 1.00 confidence with
400 distinct cities and **answers nothing at all** with 600. More history
makes fewer fields eligible.

A weight model does not need a field's values enumerated to use them as
evidence. Hash the token `city=Austin` into a fixed number of buckets and
learn a weight per bucket: model size becomes `buckets × K`, independent of
cardinality. That experiment, same datasets, 15,000 rows fitted and 5,000 held
out:

| distinct cities | current engines | hashed weights, 4,096 buckets | hashed weights, 65,536 buckets |
|---|---|---|---|
| 100 | correct at 1.00 | 1.00 | 1.00 |
| 400 | correct at 1.00 | 1.00 | 1.00 |
| 600 | **nothing at all** | **1.00** | **1.00** |
| 2,000 | **nothing at all** | 0.93 | **1.00** |
| 20,000 | **nothing at all** | 0.48 | 0.58 |

Three readings, in order of importance.

**The 600 row is the point.** Where the system today answers nothing, a hashed
weight model answers correctly and confidently, at a fixed 4,096 × 5 floats
however many cities there are. This is the only candidate in this document
that addresses the known defect from the front.

**The 2,000 row is collisions, and collisions are a dial.** 0.93 at 4,096
buckets became 1.00 at 65,536. Both model files are still constant-sized.

**The 20,000 row is not the method's fault.** 20,000 distinct cities across
15,000 training rows means most held-out cities were never seen once; nothing
can learn that, and no engine should claim to. Worth noting that the confidence
degraded roughly with the accuracy (mean 0.47 against accuracy 0.48 at 4,096
buckets), so the model was approximately honest about its own collapse before
the calibration layer touched it.

Two limits to be clear about. **Hashing helps a high-cardinality field as a
predictor, not as a target**: answering a field still needs a candidate set to
score, so `MAX_DISTINCT` still governs what can be answered. The doc's own
example is the useful half — `city` is the *source* — but half is what this is.
And a hash bucket has no name, so section 3.5's explanation problem gets worse:
the best available sentence is *"the city you typed"*, with no value-level
count behind it, unless a reverse map is kept, which puts the cardinality back
into the file.

It is also worth saying plainly that hashing is not exclusive to weights. A
frequency floor with a tail bucket — the fix
[training-and-scale.md §5](training-and-scale.md#the-distinct-value-ceiling)
already proposes — gives the conditional tables most of the same ground for
far less work and keeps the countable explanation. A weight-based engine is a
*second* reason to want bucketing, not the reason.

---

## 5. Candidate C — learned weights for the combiner, with no new engine

The cheapest weight-based method available here, and it does not add an
algorithm at all.

Every engine's final step is a weighted vote, and the weights are
hand-designed. `statistical` uses `link.strength * support_weight(rows)`
([`statistical.py:57`](../fillerai/train/algos/statistical.py)); the marginal
floor is a constant `MARGINAL_WEIGHT = 0.2`
([`base.py:290`](../fillerai/train/algos/base.py)); `tree` weights a tree by its
measured strength and penalises a path-less tree by a flat `0.25`
([`tree.py:380`](../fillerai/train/algos/tree.py)). Each of those is a
reasonable guess that has never been fitted.

**Training.** A small logistic regression per target — or one shared model
over meta-features (lambda, bucket support, how many predictors fired, how
many fields the caller typed) — fitted to predict "was this vote right". A few
dozen parameters, not 795,424.

**Inference.** Unchanged. The learned weight replaces the formula at the same
point in the same code path.

**What it costs.** Almost nothing. The calibration pass already predicts every
field of every held-out record and already knows whether each was right —
8.7 s of a default run, already spent — so the training signal exists and is
being thrown away. Model size grows by one float per link. Every
explanation survives untouched, because what changes is how loudly a reason
votes, not what the reason is.

**The one caution.** Fitting the combiner on the same holdout that calibrates
the confidence makes the confidence optimistic: the curve would then describe
rows the weights were tuned on. `split_records`
([`model.py:550`](../fillerai/train/model.py)) produces two splits, so this
needs the holdout halved, or the combiner fitted on a sub-holdout of
`context.records` as in section 3.1. Cheap either way, but it has to be done
deliberately — this is the one place where getting it wrong quietly damages
numbers users read.

Judged strictly on benefit per line of code and per second of training time,
this is the strongest candidate in the document.

---

## 6. Candidate D — boosting: weights on records rather than features

The other honest reading of "weight-based". Fit a shallow tree; increase the
weight of the records it got wrong; fit the next tree against the reweighted
data; weight each tree by how much it removed.

**Training.** Sequential, unlike `forest`, whose trees are independent, so the
rounds cannot be sampled down or reordered. That matters less than it sounds,
because section 2 showed `forest`'s cost is not in its fit at all: growing ten
trees per field over 15,000 records takes 12.7 s. The cost is downstream. An
engine carrying ten trees per target answers in 19.8 ms, and calibration pays
that for every field of every held-out record.

Which means a boosted engine inherits `forest`'s real bill rather than its
apparent one, and that the fix
[training-and-scale.md §5](training-and-scale.md#forest-at-scale) prescribes —
capping the per-tree record sample — would trim the 12.7 s and not the ten
minutes. Fewer rounds or shallower stumps is what would actually move it, and
for boosting that is a direct trade against the accuracy it exists for.

**Inference.** Nearly free to add, which is the appealing part. `TreeEngine`
already holds a list of trees per target, each with a `strength`, and already
casts them into one `Ballot`
([`tree.py:370`](../fillerai/train/algos/tree.py)). Boosting changes the fitting
loop and the meaning of `strength`; `guess`, the serialisation and the drawn
tree view carry over. Of all the candidates here this is the smallest diff.

**Pros.** Interactions natively, since the base learners are trees. Depth-two
stumps stay readable, so the explanation problem of sections 3.5 and 4 does not
apply. Reuses code that exists and is tested.

**Cons, and they are specific.** Boosting's record weights are computed from
what the previous round got wrong *with the features it had*, but D2 means the
features at serving time are a small random subset. Boost under full evidence
and the later rounds specialise in correcting errors that only occur under full
evidence — the coupling problem of section 3.6 again, arriving by a different
road. Boosting with the fractional-instance descent that makes these trees
answer from partial evidence ([`tree.py:319`](../fillerai/train/algos/tree.py))
is not a combination with a standard recipe behind it. And a boosted score is a
margin, not a probability, so `Ballot`'s deliberate choice to average rather
than multiply ([`base.py:293`](../fillerai/train/algos/base.py)) has to be
rethought for it.

Worth building *after* `forest`'s cost is dealt with as the inference-cost
problem section 2 shows it to be, not before.

---

## 7. The three to decline, and why

**Averaged perceptron.** The same shape as candidate A, cheaper per step (no
`exp`, updates only on mistakes), and it converges in fewer passes. But it
produces a margin, not a distribution, so it forfeits `Ballot.floor`, the
alternatives list, and the raw-score scale the calibration bins on. Strictly
worse than the softmax for the same work.

**A small neural network.** One hidden layer of `H` units multiplies section
2's cost by roughly `H`, putting a 22-target run into hours of pure-Python
arithmetic, with hand-written backpropagation and no autodiff to check it
against. It captures interactions, and so does a decision tree at 69 seconds.
The no-dependency rule is not the obstacle to this being *written*; it is the
obstacle to it being *usable*, and that is a cleaner reason to say no.

**Factorization machines or per-value embeddings.** Intellectually the best
answer in this document: a latent vector per `field=value` handles high
cardinality (section 4), interactions (section 3.4) and — because a sum of
embeddings is naturally robust to which terms are present — even the coupling
problem (section 3.6), all three in one mechanism. It is also the worst fit to
this project. Cost is `d`× the softmax, so eight-dimensional vectors put it in
`forest` territory at best. Nothing in it can be read: not one number, not one
sentence. And it is the one candidate that would genuinely want numpy, which
means it is the one candidate the constraint actually forbids rather than
merely taxes.

---

## 8. Recommendation

*(This was the recommendation. Section 0 says what came of it.)*

**Do candidate C first (learned combiner weights).** It is weight-based
training in the honest sense, it improves all five existing engines rather
than adding a sixth, it costs a few dozen parameters and no measurable
training time, it keeps every explanation intact, and the training signal is
already being computed and discarded. Roughly a day's work, contained to
`associate`/`base` plus the split care in section 5.

**Then candidate A+B as one engine, `linear`, if high-cardinality fields are
the goal.** Per-target softmax with hashed tokens for fields past
`MAX_DISTINCT`. Expect around 500 lines, one file, a five-minute fit at 20,000
records, and a 7 MB model file. Its real argument is section 4: it answers
fields the system currently cannot answer at all. Its honest costs are
sections 3.5 and 3.6 — no checkable explanation, and one weight set serving
every evidence size. Build it after the profiling fix that
[training-and-scale.md §5](training-and-scale.md#the-distinct-value-ceiling)
already calls for, not instead of it; the bucketing that fix needs is also
what this engine's hashing wants, and doing them in the other order means
doing the same thinking twice.

**Defer candidate D (boosting).** It is the smallest diff of any new engine and
the best of them at interactions, and it inherits `forest`'s cost — which
section 2 shows is an inference-cost problem, so the fix already written down
for `forest` would not be enough for either of them. Worth revisiting once that
is settled.

**Decline the perceptron, the network and the embeddings**, for the reasons in
section 7.

And the honest summary of what a weight-based engine is worth here:
**interactions are not the reason to build one — the trees already have those,
cheaply. Cardinality is.** The known defect is that real history is full of
fields with thousands of distinct values and the system currently drops them
in both directions. Weights are one good answer to that. Bucketing the
profiles is a cheaper one, and the two want the same groundwork.

---

## 9. Reproducing the measurements

Section 1's field counts and section 3.3's model size, from a clone:

```bash
python -m fillerai generate examples/out/claims_intake.schema.json \
    -n 20000 --seed 11 -o big.json
```

```python
import json, math, random, time
from fillerai.schema import FormSchema
from fillerai.train import features

schema = FormSchema.from_dict(json.load(open('examples/out/claims_intake.schema.json')))
recs = json.load(open('big.json'))
recs = recs['records'] if isinstance(recs, dict) else recs
fit = recs[:15000]
profiles = features.profile_all(schema, fit)
usable = [n for n, p in profiles.items()
          if p.kind in ('constant', 'enumerable') and p.filled > 0]
print(len(profiles), 'writable,', len(usable), 'usable')

rng = random.Random(7)
engine = {}
for t in usable:                       # a weight set of the right shape
    classes = [v for v, _ in profiles[t].values]
    W = {f'{s}={v}': [round(rng.gauss(0, 1), 4) for _ in classes]
         for s in usable if s != t for v, _ in profiles[s].values}
    engine[t] = {'classes': classes, 'bias': [0.0] * len(classes), 'weights': W}

blob = json.dumps({'algorithm': 'linear', 'targets': engine})
print(f'{len(blob) / 1e6:.1f} MB,',
      sum(len(e['weights']) * len(e['classes']) for e in engine.values()), 'floats')
t0 = time.perf_counter(); loaded = json.loads(blob)
print(f'load {(time.perf_counter() - t0) * 1000:.0f} ms')
```

Section 2's throughput, the sparse masked SGD loop, one target at a time:

```python
MASKS = (1, 2, 3, 5, 8)
cols = {n: features.column(fit, n) for n in usable}
rng = random.Random(0)

def epoch(target, rows):
    classes = [v for v, _ in profiles[target].values]
    index = {v: i for i, v in enumerate(classes)}; K = len(classes)
    sources = [n for n in usable if n != target]
    W = {}; bias = [0.0] * K; lr = 0.1; steps = 0
    for row in range(rows):
        truth = cols[target][row]
        if not truth or truth not in index:
            continue
        present = [s for s in sources if cols[s][row]]
        for size in MASKS:
            given = present if size >= len(present) else rng.sample(present, size)
            active = [W.setdefault(f'{s}={cols[s][row]}', [0.0] * K) for s in given]
            logits = bias[:]
            for w in active:
                for k in range(K):
                    logits[k] += w[k]
            top = max(logits); e = [math.exp(v - top) for v in logits]; m = sum(e)
            g = [x / m for x in e]; g[index[truth]] -= 1.0
            for k in range(K):
                d = lr * g[k]; bias[k] -= d
                for w in active:
                    w[k] -= d
            steps += 1
    return steps

total = 0.0
for target in usable:
    t0 = time.perf_counter(); steps = epoch(target, 1500)
    dt = time.perf_counter() - t0; total += dt
    print(f'{target:24} {len(profiles[target].values):>4} classes '
          f'{dt / max(steps, 1) * 1e6:>6.1f} us/step')
print(f'one epoch over 15,000 records, all targets: {total * 10:.0f} s')
```

Section 2's per-stage and per-engine timings:

```python
from fillerai.train import algos, derive
from fillerai.train import model as M
from fillerai.train.trace import Trace

opts = M.TrainOptions(seed=0)
fit, held = M.split_records(recs, opts)          # 15,000 / 5,000
t0 = time.perf_counter(); derive.learn_derivations(schema, fit, profiles, cols)
print(f'rules {time.perf_counter() - t0:.1f} s')

for name in ('statistical', 'tree', 'forest', 'bayes', 'nearest'):
    ctx = algos.FitContext(schema=schema, records=fit, columns=cols, profiles=profiles,
                           trace=Trace(), options=M.TrainOptions(algorithm=name, seed=1))
    t0 = time.perf_counter(); eng = algos.get(name).fit(ctx)
    fitted = time.perf_counter() - t0
    m = M.AutofillModel(schema=schema, profiles=profiles, engine=eng,
                        derivations={}, algorithm=name)
    t0 = time.perf_counter(); M._calibrate(m, held, 1)
    print(f'{name:12} fit {fitted:6.1f} s  calibrate {time.perf_counter() - t0:6.1f} s  '
          f'engine {len(json.dumps(eng.to_dict())) / 1e6:5.2f} MB')
```

(`cols` and `profiles` here are built from `fit`, as in the first block. Running
`forest` takes about ten minutes, nearly all of it the calibration call.)

Sections 3.4 and 3.6 reuse `interaction_records` from
[`tests/test_algos.py:37`](../tests/test_algos.py) — fit the loop above on
`policy`, `role` and `noise` with `relationship` as the target, once with
`masked` subsets and once on full rows, once with a token per ordered pair of
present fields and once without, then read off `predict({'policy': 'family',
'role': 'dependent'})` and `predict({'policy': 'family'})`. The two-class
variant replaces the `answers` map with
`{('family','claimant'): 'yes', ('family','dependent'): 'no',
('single','claimant'): 'no', ('single','dependent'): 'yes'}`, leaving the four
cells otherwise identical.

Section 4 is the cardinality experiment from
[training-and-scale.md §6](training-and-scale.md#6-reproducing-the-measurements),
with the model replaced by hashed weights:

```python
import math, random, time, zlib
STATES = ['TX', 'CA', 'NY', 'FL', 'IL']; BUCKETS = 4096

def run(n_cities, rows=20000, epochs=3, lr=0.3, seed=3):
    rng = random.Random(seed)
    cities = [(f'City{i}', STATES[i % 5]) for i in range(n_cities)]
    data = [cities[rng.randrange(n_cities)] for _ in range(rows)]
    fit, held = data[:15000], data[15000:]
    K = len(STATES); idx = {s: i for i, s in enumerate(STATES)}
    W = [[0.0] * K for _ in range(BUCKETS)]
    for _ in range(epochs):
        for city, state in fit:
            w = W[zlib.crc32(('city=' + city).encode()) % BUCKETS]
            top = max(w); e = [math.exp(v - top) for v in w]; m = sum(e)
            g = [x / m for x in e]; g[idx[state]] -= 1.0
            for k in range(K):
                w[k] -= lr * g[k]
    hits = conf = 0
    for city, state in held:
        w = W[zlib.crc32(('city=' + city).encode()) % BUCKETS]
        top = max(w); e = [math.exp(v - top) for v in w]; m = sum(e)
        best = max(range(K), key=lambda k: e[k])
        conf += e[best] / m; hits += STATES[best] == state
    return hits / len(held), conf / len(held)

for n in (100, 400, 600, 2000, 20000):
    print(n, '%.2f accuracy, %.2f mean confidence' % run(n))
```

All figures: Python 3.11, single core, the same machine and form as
[training-and-scale.md §4](training-and-scale.md#4-what-happens-at-20000-records).
Treat them as relative costs.
