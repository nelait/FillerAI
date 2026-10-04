# Pending and known items

What is missing, what is known to be broken, and what was deliberately not
built. Each entry says what it is, what the evidence is, and what it would
take — so that picking one up does not start with a re-investigation.

Verified against version 0.11.0 on 2026-09-22; the bot items and §2.4 against 0.15.0 on 2026-09-27. Where something was measured
for this document, the command is given.

**Status key:** 🔴 blocking a claim this project makes · 🟠 a real defect with
a known workaround · 🟡 worth doing · ⚪ deliberately not done.

---

## 1. Unfinished work

### 1.1 🔴 The LLM acceptance gate has never been run

`livetests/test_rules_acceptance.py` holds the gate that was written down in
[llm-implementation-plan.md](llm-implementation-plan.md) *before* the feature
was built: `claims_intake.html` declares no rules and saves 5.0% of an agent's
work, and proposing rules has to move that number without making the generated
data incoherent. If it does not, the honest thing is to delete the feature.

**It has never been run, on either service**, because no session has had an
API key. Everything in `tests/` replays a **hand-authored** fixture — the
fixtures say so in a `_note` — which exercises every branch of the validation
gate and says nothing whatsoever about whether a real model proposes good
rules.

**What it takes:** a key, and one command.

```bash
FILLERAI_LLM_LIVE=1 FILLERAI_LLM_KEY=sk-… python -m unittest discover -s livetests
```

`livetests/record.py <form> [provider]` records a real exchange to replace the
hand-authored fixture; the non-default provider's recording is suffixed, so
recording one never clobbers the other.

This is the top of the list because it is the one open item that could
invalidate a shipped feature.

### 1.2 🔴 Nothing has been trained on real past submissions

Every number this project reports was measured on data it invented, and
[assumptions.md](assumptions.md) §3.3 is why that caps the result. Measured on
five example forms (reproduce with [process.md](process.md) §0):

| Form | Declared rules | Saving |
|---|---|---|
| `claims_intake.html` | 0 | 3% |
| `patient_registration` | 0 | 7% |
| `member_enrollment` | 0 | 7% |
| `auto_insurance_quote` | 27 | 23% |
| `employee_onboarding` | 31 | 32% |

The six engines land within a few points of each other on all of them, because
the ceiling is the information in the data rather than the quality of the
model. A decision tree that can represent "family policy *and* dependent
claimant therefore relationship is child" has nothing to represent when the
generator never put such a rule in.

**What it takes: real records, and nothing more in the pipeline.** Since
0.19.0 the way in exists: upload an export on the Data step (or `fillerai
clean`), and it is mapped, cleaned and kept as a real dataset; Train learns
from it, and the Simulate step's **Test on real records** scores any model on
it. See [real-data.md](real-data.md). What is still missing is the data
itself and somebody running it inside the environment that holds it.

Until that happens, the honest claim for this project is "3–32% on synthetic
data, depending entirely on how much of the form's business logic was written
down", not a single headline number.

### 1.3 🟡 LLM phases 2, 3 and 4 are not built

Phases 0 and 1 shipped (the fence, the transport seam, `propose-rules` /
`apply-rules`, the validation gate). The plan's remaining phases are designed
and unbuilt:

- **Phase 2 — semantic typing as a disagreement report.** Ask a model to type
  the fields, show it *against* what `infer.py` decided, and only where they
  disagree. Worth doing: an opaque form where every control is `field_0417` is
  exactly where inference is weakest ([assumptions.md](assumptions.md) §2.3).
  Note the failure mode already written up: a wrong semantic type is silent —
  it generates plausible values of the wrong kind rather than erroring.
- **Phase 3 — an `llm` engine**, to settle the prediction question with a
  measurement rather than an argument. [llm-modelling.md](llm-modelling.md)
  predicts it is not worth it (only 6.8% of the form is headroom any model
  could win, and 89% of what is left blank is closed to all of them), and the
  phase exists to test that prediction rather than assume it.
- **Phase 4 — free text.** The narrative box on a claims form, which nothing
  here attempts today.

Each phase has a measured gate it must clear before the next starts; §6 of the
plan has the order.

### 1.4 ✅ Postgres (done in 0.18.0)

`PostgresDatabase` in `fillerai/db.py`, with psycopg as the optional extra
`fillerai[postgres]`. `tests/test_postgres.py` reruns the database-backed
suites against a real Postgres when `FILLERAI_TEST_POSTGRES_URL` is set;
without it, only its driver-free checks run.

### 1.5 🟡 The effort constants have never been measured

Five constants in `fillerai/simulate/effort.py` produce every "X% less work"
this project has ever printed, and all five are considered guesses
([assumptions.md](assumptions.md) §4). Reports print them underneath
themselves rather than hiding them, which is the right handling of an
assumption but not a substitute for measuring it.

**What it takes:** a stopwatch on ten real forms. `Effort` already takes all
five as arguments, so better numbers substitute without a code change.

### 1.6 🟡 One model averages every agent

Two people working the same queue develop different habits and the model mixes
them. With the library recording which dataset each model came from, per-agent
models are a matter of keeping the datasets apart rather than a change to
anything — but nothing does it today.

### 1.7 🟡 The Library panel does not show a comparison

The library already knows that four models descend from one dataset
(`store.descendants`). Showing them as a table in the Library panel, rather
than four rows that happen to share a parent, is the last step in making
`--compare` something you come back to rather than something you run once.

### 1.8 🟡 The bot's language-model reader has never met a model

`serve --bot-llm` hands chat phrases to `fillerai/llm/understand.py`, and
every test of it replays a canned answer (`tests/test_llm_understand.py`). What
is tested is that a bad answer is not believed and a failure falls back to the
local reading; whether a real model reads "we're moving in with my sister in
Tustin" well is not. Same missing key as §1.1.

### 1.9 🟡 The microphone has only been tested as far as the text it produces

`SpeechInput` wraps the browser's own recogniser, and the only browser this
project is checked in is headless and has no microphone. The service side of
speech (`via: "speech"`, alternatives, no example hints in a spoken question)
is tested. A person talking into Chrome has now worked once, by hand, on
2026-09-27, after an early failure turned out to be Chrome listening to a
different microphone (an iPhone over Continuity), not a fault here; there is
still no automated check of it. `--bot-transcribe` (the
recording route, for when Chrome's speech service is blocked) is tested end to
end with a recorded tone through a stand-in for OpenAI, but has not yet
transcribed a real voice with a real key.

### 1.10 🟡 The bot is English and one request at a time

Its field words, action phrases and questions are English, and a message
that asks for two things is not split; see §2.5 for what actually happens and
[bot-builder.md](bot-builder.md) §9.

---

## 2. Known defects

### 2.1 🟠 The test suite races on the in-memory SQLite backend

`tests/test_web_auth.TestSeparateLibraries.test_a_model_trained_on_the_worker_lands_in_the_trainer_s_library`
fails intermittently with `database table is locked`.

**Measured for this document: 4 failures in 15 runs.** Reproduce with

```bash
for i in $(seq 1 15); do python -m unittest tests.test_web_auth.TestSeparateLibraries 2>&1 | tail -1; done | sort | uniq -c
```

**It is pre-existing**, not from any recent change — confirmed previously by
running the same loop in a worktree at `main` commit `1ca0f50`.

**Cause.** `SQLiteDatabase.__init__` gives the `:memory:` backend a
`cache=shared` URI so thread-local connections see one database
(`fillerai/db.py:361`). Shared cache raises `SQLITE_LOCKED` where a file
raises `SQLITE_BUSY`, and **`busy_timeout` does not apply to
`SQLITE_LOCKED`** — the loser fails immediately instead of waiting. The
training worker thread writing the model and the request thread polling
`/api/train/log` collide outright.

**No user is affected.** Only the `:memory:` path takes shared cache; a real
file database gets `PRAGMA journal_mode = WAL` in the same `_connect`, where
readers never block the writer. But the repo has no CI (§2.2), so the local
suite is the only signal, and this is noise in it.

**Do not reach for `PRAGMA read_uncommitted = 1`.** It was tried. It removes
the reader's lock, but the *writer* still takes `SQLITE_LOCKED` against the
reader's table lock, and the failure rate got worse (6 of 25).

**Two fixes that would work:**

1. Serialise writes on the in-memory backend behind a `threading.Lock` held
   across `execute()` and `transaction()` when `self.memory`.
2. Drop shared cache and give the in-memory backend a temp file, so it gets
   WAL like every other path.

The second is probably the better trade: it makes the test backend behave like
the real one, which is what a test backend is for.

**Offered as a separate change on 2026-09-22; not yet picked up.**

### 2.2 🟠 There is no CI

No `.github/workflows`, so no pull request in this repository will ever show a
green check, and `python -m unittest discover -s tests -q` run locally is the
only signal that anything works. 868 tests, about 70 seconds.

Combined with §2.1, a contributor who runs the suite once and sees a failure
has no way to tell a real regression from the known race without re-running.

**What it takes:** a workflow that runs the suite on 3.10 through 3.13. It
would need §2.1 fixed first, or it will be red about a quarter of the time.

### 2.3 🟡 A wrong semantic type fails silently

If `infer.py` types a field wrongly — not `unknown`, but *wrong* — generation
produces plausible values of the wrong kind and every stage downstream
believes them. Nothing errors and nothing warns.

`inspect --evidence` and `--review-below` are the mitigation, and they require
somebody to look. LLM phase 2 (§1.3) is the designed fix.

### 2.4 🟡 Small inconsistencies found while writing the reference

Each is minor and none loses data; they are listed so nobody re-discovers
them. Found by reading the code, calling the routes and running the commands
for [reference/http-api.md](reference/http-api.md) and
[reference/cli.md](reference/cli.md).

- **`/api/bot/turn` and `/api/bot/transcribe` drop the error `code`.** They
  turn the `/v1` error into the `/api` shape, so the Bots tab gets the status
  and the sentence but not `stale_action` and the rest.
- **`account_off` (403) cannot be reached in practice.** Disabling a user
  revokes their tokens in the same step, so the token fails first with
  `401 bad_token`. [integration.md](integration.md) now says so.
- **The sample application accepts template keys `/v1` does not.** Its check
  is `^[a-z0-9][a-z0-9_-]{0,63}$`; `/v1` wants `^[a-z][a-z0-9_]{0,63}$`, so a
  key with a hyphen or a leading digit is forwarded and comes back `404`.
- **A wrong method answers differently on the two surfaces.** A `GET` on an
  `/api` POST route gets a plain-text `404`; `/v1` answers
  `405 method_not_allowed` in JSON.
- **`/api/auth/me` under `--no-auth` answers `401 sign in first`**, while
  every other `/api` route works without signing in.
- **What the CLI saves is not in the server's library straight away.** `bot
  add`, `library` and `--save` write the file library; with accounts on,
  `serve` uses the database and copies new file-library entries into the first
  administrator's library only when it starts.
- **`check` reads JSON only**, and fails with a traceback on a CSV that
  `train`, `evaluate` and `simulate --records` would accept.
- **`train --compare` ignores `-o`, `--save`, `--script`, `--tree` and
  `--evaluate`** without saying so, and **unknown `--set` keys** are ignored
  silently too.
- **`library list -k KIND`** says the library is empty when only the filter
  matched nothing, and the kind `template` runs into the date column.
- **A missing input file, or a port already in use for `serve`,** ends in a
  Python traceback rather than a one-line message.

### 2.5 🟠 The chat's local reader: defects found while documenting it

Found by running the reader for [nlp-and-chatbot.md](nlp-and-chatbot.md) §8,
against 0.15.1. None loses data the person did not see, because every value
is shown before anything is submitted, but the first is visible to users.

- **A second request becomes a value.** "change my address and send me an
  ID card" fills the street address with "and send me an ID card"
  (reproduce with `fillerai bot chat` and the two starters). There is no
  clause splitting, and "address" is a default word for the street field.
- **"ok", "okay" and "please do" never confirm**, although they are in the
  confirm list: politeness words are stripped before the lookup.
- **"new" never counts as a change word**, because it is a stop word and is
  removed first.
- **Speech alternatives are rarely read.** They are tried only when the first
  transcript finds no template and no values at all.
- **The stemmer misses common pairs** ("update"/"updated", "moving"/"moved").
- **A field without options accepts any word of the right shape**, so "Mars"
  is a state in the starter template; the docstring of
  `fillerai/llm/understand.py` says otherwise.
- **`fillerai bot chat` always reads locally**, even when `--bot-llm` is
  configured for the server.

The first two are small code changes with tests; the rest are listed with
options in [nlp-and-chatbot.md](nlp-and-chatbot.md) §10.

---

## 3. What is deliberately not there

These are not oversights, and a change request for one of them should be
argued rather than assumed.

- ⚪ **No dependencies, no build step.** `dependencies = []` is asserted by a
  test, because it is a promise about where this can run
  ([assumptions.md](assumptions.md) §1.2).
- ⚪ **No network in the core.** One module opens a socket, behind an import
  fence with a test that checks it three ways.
- ⚪ **No npm package.** One dependency-free ES module shipped inside the
  Python package, so an application fetches it from the service it talks to
  rather than vendoring a copy that drifts.
- ⚪ **No audit log.** Accounts exist; a record of who did what does not. The
  schema has room for it and nothing needs it yet.
- ⚪ **No rate limiting, and no hardening for a public deployment.** `serve`
  binds to loopback and says so loudly if asked otherwise; `--no-auth` is
  localhost-only because anyone who can reach the port is then signed in.
- ⚪ **No drift detection and no retraining trigger.** The model assumes the
  form's relationships are stable over the period being modelled
  ([assumptions.md](assumptions.md) §2.6).
- ⚪ **No repeating sections or conditional visibility** in the schema. A form
  with "add another dependant" has to be flattened into numbered fields, and
  the model will treat them as unrelated columns
  ([assumptions.md](assumptions.md) §2.1).
- ⚪ **The sample application has no sign-in of its own.** It is a demo;
  whoever reaches its port is the demo customer
  ([security.md](security.md) §9).
- ⚪ **The file library has no owners, search, tags, concurrent writers or
  garbage collection** beyond `prune`. Each would be a good idea in a shared
  service, which is what the database store is for.

---

## See also

- [architecture.md](architecture.md) — where each of these lives.
- [assumptions.md](assumptions.md) — the constraints several of these follow
  from.
- [llm-implementation-plan.md](llm-implementation-plan.md) §6 — the gates for
  the unbuilt phases.
