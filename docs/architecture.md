# Architecture

What the pieces are, which way they point, and the boundaries that are not
allowed to move. The README is the tour; this is the map you want open when
you are changing something and need to know what else it touches.

Everything here was checked against the code at version 0.19.0.

---

## 1. The shape of it

AIrForms is four stages over one shared contract, with a library underneath
and two HTTP surfaces on top. Real records, when there are any, come in beside
`generate` (§3, realdata) and leave every stage after it unchanged.

```
      a form                                              an agent
   (HTML or a field spec)                             (or somebody's app)
         |                                                    ^
         v                                                    |
   +-----------+     +------------+     +---------+     +------------+
   |  extract  | --> |  generate  | --> |  train  | --> |  simulate  |
   +-----------+     +------------+     +---------+     +------------+
         |                  |                |                 |
      schema             records           model             a run
         |                  |                |                 |
         +------------------+----------------+-----------------+
                                   |
                            +-------------+
                            | the library |   every entry points at what
                            +-------------+   it was made from
                                   |
                     +-------------+-------------+
                     |                           |
              a directory of JSON          the database
              (fillerai.store)           (fillerai.dbstore,
               one person, CLI            owners, accounts)
```

The arrow that matters most is the one that is **not** there: nothing flows
backwards. `generate` never sees the training code, `train` never sees the
markup, `simulate` never sees the generator. Each stage knows only the schema
and the artefact the stage before it produced. That is what lets a dataset of
real past submissions be dropped into `train` with no change anywhere — a
dataset is a dataset, whoever made it. Since 0.19.0 there is a way in for one:
`fillerai/realdata.py` turns an export into records of exactly the shape
`generate` writes.

### The stages, in one line each

| Stage | Input | Output | Where |
|---|---|---|---|
| **extract** | an HTML page, a JSON field spec, or a file of records | a `FormSchema` | `fillerai/extract/`, `fillerai/infer.py` (`realdata.spec_from_table` for a file) |
| **generate** | a `FormSchema` | coherent synthetic records | `fillerai/generate/` |
| *or* **realdata** | a `FormSchema` + an export (CSV, JSON, NDJSON) | cleaned real records, same shape | `fillerai/realdata.py` |
| **train** | a schema + records | an `AutofillModel` | `fillerai/train/` |
| **simulate** | a model + forms it has not seen | a costed run | `fillerai/simulate/` |

---

## 2. The contract: `fillerai/schema.py`

One dataclass tree — `FormSchema` → `Screen`, `Field` → `Option`,
`Constraints`, `Derived` — serialised as JSON. It is the only thing every
stage agrees on, so it is the only file where a change is expensive.

Three stability rules are written into the module and enforced by the code:

- **`SCHEMA_VERSION` (currently `"1.0"`) is bumped on a breaking change.**
  `FormSchema.from_dict` refuses a schema whose *major* version differs, with
  a message naming both versions, rather than half-loading it.
- **Consumers ignore unknown keys.** Every `from_dict` filters to the keys it
  knows, so adding an optional key is additive and safe across versions.
- **`Field.name` is the stable join key** across schema, dataset, model and
  run. It is the form control's `name` where there is one, otherwise a slug
  from the id or label.

Two parts of a `Field` are worth naming separately because the rest of the
system keys off them.

**`semantic_type`** is what the field *means* — `postal_code`, `claim_number`,
`diagnosis_code` — as opposed to its data type. There are 47 of them in
`SEMANTIC_TYPES`. Generation is driven entirely by this, so supporting a new
kind of field means a member here plus a renderer in
`fillerai/generate/render.py`, and nothing else. Each one arrives with a
`confidence` and an `evidence` list, so a weak guess can be reviewed rather
than silently believed.

**`derived`** is a rule the form declares about itself: `sources` names the
fields this one follows, `table` maps their joined values to what it may then
hold, `otherwise` covers a combination the table does not mention. Values
join with `|` in the order `sources` gives, lower-cased and stripped, because
the rule and the option list are written by different people at different
times. A list of several values is a *narrowing* ("Engineering means one of
these four titles"); one value is a *determination* ("Platinum means a 250
deductible"). Both are worth carrying, and the generator honours both.

A second versioned format sits beside it: `MODEL_VERSION` in
`fillerai/train/model.py`, currently `"2.0"`, bumped when the engine became
selectable. A 1.x model still loads — it was necessarily fitted with
conditional tables, so its links read straight into that engine.

---

## 3. The stages

### extract — `fillerai/extract/`, `fillerai/infer.py`

Two ways in, one way out.

`html_form.py` does **structural work only**: find the controls, pair them
with their labels, group the radios, read the declared constraints. It runs on
`dom.py`, a minimal tree built over `html.parser` — streaming events cannot
answer "what encloses this control", which label association, fieldset scoping
and `aria-describedby` all need.

`spec.py` takes a compact hand-written JSON field spec and produces the same
`FormSchema`. It exists because the case AIrForms was built for is a team that
*cannot share their markup*. Anything the spec states is taken as given;
anything it omits is inferred exactly as it would be from HTML. `follows` /
`when` / `otherwise` in a spec become a `Derived` on the field.

A third, for when there is no form at all, only a file of records:
`realdata.spec_from_table` writes a field spec from the columns (a name per
column, and only what the values can prove about type and options), and the
spec reader and inference take it from there. `fillerai extract export.csv`
and the Source step's **Records (CSV)** tab both go this way.

`infer.py` then decides what each field *means*, from signals ranked by how
much they can be trusted:

| Signal | Confidence | Why there |
|---|---|---|
| `autocomplete` token | 0.97 | a spec-defined token somebody authored deliberately |
| `<input type>` | 0.90 | the markup naming its own meaning |
| control `name` | 0.80 | usually the developer's own word for the field |
| label text | 0.74 | written for a person, not a machine |
| the option list | 0.72 | 50 two-letter values is a state dropdown |
| placeholder, inputmode, pattern | 0.55 | weak, and marked as weak |

The rules are ordered most-specific first, so "date of birth" settles before
the generic "date" rule is reached and "company name" never falls through to
"name". Every decision keeps its evidence.

### generate — `fillerai/generate/`

**The unit of generation is a persona, not a field.** `persona.py` invents one
imaginary entity — their name, their addresses, their employer, their account
identifiers — and every field in the record is rendered by asking the persona
for the fact it wants. Coherence falls out of that instead of being patched up
afterwards: the city sits in its own state, the area code matches where the
person lives, the email is built from the name above it. `catalogs.py` stores
geography as coherent tuples rather than parallel lists for the same reason.

A form that asks for the same kind of fact twice — billing and shipping — marks
those as separate `group` values at extraction, and the persona keeps one
address per group, so within a group everything agrees while groups stay
independent.

`render.py` decides how a particular *control* wants to receive a fact the
persona already holds: pick the matching option out of a `<select>`, format the
date the way the placeholder implies, trim to `maxlength` without producing
something the form would reject.

`dataset.py` assembles records, resolves declared rules in dependency order,
applies the blank rate, and can `validate` and `coherence_report` what it
produced.

By default identifiers **cannot belong to a real person**: SSA has never
issued an area number above 899, `555-0100`–`555-0199` is reserved for
fiction, email domains are the RFC 2606 documentation domains, card numbers
are Luhn-valid but from the published test IIN ranges.
`--realistic-identifiers` turns that off for a downstream validator that needs
real shapes, and the output should then not leave a controlled environment.

### realdata — `fillerai/realdata.py`

Real records, sitting on the same side of the schema as `generate` and
standing in for it. Three steps, shared by the UI (`/api/data/read`,
`/api/data/clean`, `/api/data/schema`), the CLI (`fillerai clean`) and the
tests: **read** a CSV, JSON list or NDJSON with every cell as text; **map**
each column to a field by name or label, which the person checks; **clean**
with eight named fixes (`FIXES`), each switchable and each reporting what it
changed. The output has one key per fillable field, in the form's order,
written the way the generator writes it — it borrows `generate.render`'s date
format to make sure — so `train`, `evaluate` and `simulate` take it without
knowing where it came from. The server holds nothing between calls: the
browser sends the file each time. A saved set is an ordinary `dataset` entry
with `origin: real` in its metadata. `/api/evaluate` (the Simulate step's
**Test on real records**) scores a model against any dataset. Details and
limits: [real-data.md](real-data.md).

### train — `fillerai/train/`

This is the largest subsystem, and its architecture is one idea: **a shared
layer around one swappable engine.**

Three things can answer for a field, in this order:

1. **A rule** (`derive.py`) — a full name is its parts joined, a middle
   initial is one letter, an age is arithmetic on a date of birth, "same as
   above" is a copy. Each rule is *proposed* from the semantic types and then
   **checked against the training data**, kept only if it actually held. The
   semantic type says where to look; the data says whether it is true. Rules
   are arithmetic, not statistics, so they sit **above** the choice of
   algorithm.
2. **The engine** — the only selectable part.
3. **The field's usual value** (`USUAL_FLOOR = 0.25`) — always mixed in at a
   low weight (`MARGINAL_WEIGHT = 0.2`) so thin evidence backs off to the
   common answer instead of committing to a bucket of two rows.

And a fourth outcome that is an answer rather than a failure: the field is a
claim number, it is different in every record, and the honest prediction is
none at all.

**What stays shared** is the interesting part, because it is what makes the
engines comparable: profiling the columns (`features.py`), verifying the rules,
holding a quarter of the records back (`holdout=0.25`), calibrating the
confidence against them, and scoring the result. A confidence of 0.8 is
measured the same way and means the same thing whichever engine produced it,
so changing the dropdown changes the model and not the yardstick.

The six engines live in `fillerai/train/algos/`, registered by importing the
package:

| Name | What it is | Why it is there |
|---|---|---|
| `statistical` (default) | conditional tables, combined by a weighted vote | every number in it is checkable by hand, and it degrades gracefully from one typed field |
| `tree` | one decision tree | a relationship that only exists in a *combination* is invisible to a vote of single-field opinions |
| `forest` | ten bagged trees | one tree can only root on one question; a stand covers whatever the agent happened to type |
| `nearest` | the most similar past records, weighted per target | this is literally what an experienced agent does |
| `bayes` | naive Bayes | the first thing anyone asks for, and the clearest demonstration of what calibration is for |
| `linear` | softmax regression over hashed evidence | the only one that can use a high-cardinality field as evidence |

Adding a seventh means one module, one `Algorithm` registered at the bottom of
it, and one line in `algos/__init__.py`. Nothing above the package changes.

Three supporting modules matter to the architecture rather than to any one
engine:

- `associate.py` — Goodman and Kruskal's lambda, leave-one-out, against a
  shuffled baseline. The careful measure, and it costs most of a fit.
- `algos/relevance.py` — one pass of Quinlan's gain ratio. The cheap
  shortlist, for the two engines making a coarser decision.
- `algos/combine.py` — how loudly each voter speaks, fitted rather than
  guessed, on a slice taken off the *front* of the holdout so the calibration
  is never measured on rows the weights were chosen for. Kept only if it beats
  the hand-picked weights on rows neither saw.

Two cross-cutting pieces come out of a run beside the model: `trace.py`, an
append-only log with a lock and a cursor, which the web UI polls while a
worker thread fits; and `script.py`, real runnable Python generated from the
same options object the run was given, so it cannot describe a run that did
not happen.

### simulate — `fillerai/simulate/`

`form.py` rebuilds the form **from the schema**, not from the markup it was
extracted from. Two reasons, both deliberate: putting user-pasted markup back
on the page means running whatever came with it, and a sandbox strict enough
to be safe is too strict to type into; and rebuilding makes the stage work
identically for a form that arrived as a field spec — which is the case that
exists precisely because the markup could not be shared.

`effort.py` holds every assumption behind every saving this project reports,
in one frozen dataclass, and `Effort.assumptions()` returns them in the words
a report prints. See [assumptions.md](assumptions.md) §4.

`run.py` is a pure function of the model, what has been typed, and the case.
Nothing is remembered between calls, so the UI can ask for a fresh board on
every keystroke and the CLI can ask for one per record, and the two cannot
disagree about what the model thinks. It answers three questions rather than
one: what got filled (including what it *declined*, which is a result), whether
it was right (only answerable against a case whose true contents are known),
and what that saved — with the review of every filled value and the cost of
correcting the wrong ones charged **against** the saving.

---

## 4. The library — `store.py`, `dbstore.py`

Every stage writes what it produced, and every entry records what it was made
from. A model points at its dataset, the dataset at its schema, the schema at
the markup or spec it was read out of. Follow the chain up for provenance;
follow it down from a source for every model that descends from it. The same
links read in either direction.

Six kinds, with id prefixes: `source` (`src-`), `schema` (`sch-`), `dataset`
(`dat-`), `model` (`mdl-`), `script` (`scr-`) and the bot's `template`
(`tpl-`). A dataset of real records is still a `dataset`; it says so with
`origin: real` in its metadata. The formats of each are in
[reference/data-formats.md](reference/data-formats.md).

There are **two implementations of the same interface**, and a caller holding
one cannot tell which it has — same `Entry`, same ids, same method names.
That is what let the web server keep every endpoint it already had when
accounts arrived.

- `store.Store` — a directory, one folder per kind, **two files per entry**: a
  small one with metadata, a large one with the payload. Listing reads only
  the small ones, so it stays instant with a thousand entries. No index to
  rebuild, no lock, no migration, and you can `cat` any of it. This is what
  the CLI uses by default.
- `dbstore.DatabaseStore` — the same library in SQL, with **an owner on every
  entry** and **writes that either happen or do not** (metadata and payload go
  in under one transaction). An entry is named by `(id, owner)` together, so
  two people who import the same directory get the same ids — the ids are what
  carry their lineage — and neither can reach the other's copy.

`fillerai db import` copies a directory into the database with ids and lineage
intact.

---

## 5. Storage — `db.py`

SQLite by default, in the standard library, so "nothing to install, nothing
that leaves the machine" survives having a database. One file you can open
with `sqlite3`.

Everything goes through `Database`, and since 0.18.0 there are two
implementations: `SQLiteDatabase` and `PostgresDatabase`. `connect()` picks
one from the URL (`--database`, or `FILLERAI_DATABASE_URL`): `sqlite://…` or a
plain path, or `postgresql://…`. Postgres goes through psycopg 3, the optional
extra `fillerai[postgres]`, imported only when such a URL is opened; without
it the error says how to install it. What is actually backend-specific is
small enough to name, and that is the whole justification for the class:

- **the parameter style** — callers write `?`; a backend wanting `%s` rewrites
  the statement on the way through;
- **connecting** — a URL in, a DB-API connection out;
- **nothing else.** The DDL in `MIGRATIONS` uses the SQL both accept — `TEXT`,
  `INTEGER`, `CREATE TABLE IF NOT EXISTS` — and times are ISO 8601 strings
  rather than a timestamp type, because the two disagree about timezones in a
  way not worth a translation layer.

`MIGRATIONS` is a list of `(version, [statements])` applied in order, once, and
recorded. **Never edit a step that has shipped: add another.** Two steps exist
today — step 1 (`users`, `sessions`, `entries`, `payloads`, `settings`) and
step 2 (`api_tokens`).

**One connection per thread.** The web server is threaded and a SQLite
connection is not safe to share. Each thread opens its own and keeps it; WAL
lets readers run while a write is in flight, and a busy timeout covers a
collision. (The in-memory backend used by tests takes a different path and has
a known race — see [pending.md](pending.md) §2.1.)

`PostgresDatabase` differs in two places, both inside the subclass: a query
outside `transaction()` commits (or rolls back) straight away, so no idle
transaction holds a snapshot; and a finished request hands its connection
back to a small pool (`POOL = 8`) rather than dropping it.
`tests/test_postgres.py` reruns the database-backed suites against a real
server when `FILLERAI_TEST_POSTGRES_URL` is set.

---

## 6. Who is asking — `auth.py`, `tokens.py`, `web/keyring.py`

Two kinds of credential, deliberately not the same thing.

**A person** (`auth.py`) gets a password hashed with `hashlib.scrypt` at
interactive parameters — about 45ms and 16MB per attempt, nothing to a person
logging in and a great deal to somebody working through a stolen table. PBKDF2
is kept as a fallback for a Python built without scrypt, and both formats
verify on the way back in. **Sessions live in the database, not in the
cookie**: the cookie carries a random id and a signature over it. The
signature is not what makes it safe — the id is random and looked up
server-side either way — it is what lets a forged or stale cookie be thrown
out without touching the database. Signing out deletes the row, so it takes
effect everywhere at once. **The last administrator cannot be removed** — not
disabled, not demoted, not deleted.

**An application** (`tokens.py`) gets a bearer token, `flr_<id>_<secret>`, with
24 random bytes from `secrets` (32 URL-safe characters). The secret is hashed with plain **SHA-256, not
scrypt**, and that is a considered difference: a password is a short string a
person chose, so the hashing cost is the defence; a token's secret is beyond
guessing and is verified on *every* API call, where 45ms would make the
surface useless for the thing it exists for. The id travels with the secret so
verification is one indexed lookup and a constant-time compare. Tokens follow
the account: disabling or deleting a user revokes them.

**A third-party API key typed into the UI** (`web/keyring.py`) is held in the
server process, in a dictionary, keyed by user id, **and written nowhere** —
not the database, not the file library, not a config file, not the log.
Restarting forgets every one, and the UI says so where the box is. Anybody
who wants it to survive a restart has the environment variable, which is where
a secret belongs.

---

## 7. The two HTTP surfaces — `web/server.py`, `web/rest.py`

One port, one handler (`Handler` in `web/server.py`), and these paths:

| Path | What | Who |
|---|---|---|
| `/` | the product page (`static/product.html`) | anyone |
| `/app` | the app itself (`static/index.html`); `/index.html` answers `302 /app` for old bookmarks | signed in, else `302 /login` |
| `/login` | the sign-in page | anyone |
| `/docs`, `/docs/<slug>` | the user guide (`web/guide.md`), the README and every Markdown file in `docs/`, rendered by `web/docs.py` | an access code an administrator sets in Settings; none set, and the page says so |
| `/static/…`, `/client/…` | the UI's assets; the browser client and its demo pages | anyone (they are inert) |
| `/sample/…` | the sample application, proxied in-process to the port it also listens on | signed in, unless `--sample-public` / `FILLERAI_SAMPLE_PUBLIC=1` |
| `/api/…` | the UI's own endpoints, one POST each | session cookie + CSRF header |
| `/v1/…` | the integration API | bearer token |

The help panel on the right edge of every screen is the same `guide.md`, cut
into one section per screen and served by `/api/help`, so the in-app help and
`/docs` cannot drift apart. The docs access code is hashed like a password
and its cookie is separate from the session's.

The last two are the surfaces that matter here. They are routed by the same
handler and share nothing but the library underneath. Conflating them would
undo the reason the second one exists.

| | `/api` | `/v1` |
|---|---|---|
| for | the AIrForms UI talking to its own server | somebody else's application |
| credential | session cookie + CSRF header | **bearer token only** |
| addressing | an in-process `model_id` handle | a library id (`mdl-…`), which survives a restart |
| method | one POST per button | GET for reads, POST for work |
| stability | may change with the UI | may not change without the version changing |

`Handler._rest` installs a **fresh empty `Context`** before dispatching to
`/v1`, so the cookie is not read there at all. A surface that honoured the
cookie could be driven by any page open in the user's browser, which is the
entire reason for a second credential.

CORS follows from whether there is a credential (`allowed_origins()`): with
accounts on, the default is `*`, which is safe because a bearer token is
required either way and `Allow-Credentials` is never sent; with `--no-auth`,
**no origin** is allowed until `--cors-origin` names one, since `*` with no
credential opens the library to any page the user has open.

The bot service is on the same surface - `/v1/templates` and
`/v1/bot/turn`, in `web/botrest.py` - and the UI's Bots tab runs the very same
turn through `/api/bot/turn`. See [bot-builder.md](bot-builder.md).

`rest.py` holds **no modelling of its own** — it calls the same `predict()`
the Train panel does — which is the point of it existing rather than being
bolted onto `server.py`. Endpoints: `GET /v1/health` (no token),
`GET /v1/models`, `GET /v1/models/{id}`, `POST …/suggest`, `POST …/fill`,
`POST …/batch`. Models are cached per `(owner, entry id)`; entries are
immutable so none goes stale, and deleting one calls `rest.forget()`.

The browser client is **one dependency-free ES module** at
`fillerai/web/static/client/fillerai.js`, shipped *inside the package*, so an
application fetches it from the service it talks to rather than vendoring a
copy that drifts. There is no npm package on purpose. Full reference:
[integration.md](integration.md).

The UI itself is `static/index.html`, `app.js`, `styles.css` (and
`product.*`, `login.*`, `docs-gate.js`, `docs.css` for the pages around it) —
**no build step**, which is the same promise as everything else here.

**The `/sample/` proxy** is the one place outside the LLM fence where the
server makes a request of its own: `Handler._sample` forwards to the sample application on `127.0.0.1` with
`http.client`, so a host that exposes one port (Railway) still shows it. The
sample application itself reaches AIrForms over `/v1` with `urllib`, as any
host application would.

---

## 8. The optional LLM features — `fillerai/llm/`

Off by default, and fenced off structurally rather than by convention.

**Nothing under `fillerai/` imports this package at module scope.** The CLI
commands that use it import it inside the function that runs them.
`tests/test_llm_fence.py` fails if that stops being true, and checks it three
ways: a **subprocess** that imports every core module and asserts
`fillerai.llm` is absent from `sys.modules` (a same-process check would pass or
fail on test ordering), a textual scan for `.llm` in import lines outside the
package, and an assertion that `dependencies = []` is still in
`pyproject.toml`.

That is stricter than "an optional dependency" on purpose: the promise that
nothing here talks to a network is what lets AIrForms run inside the
locked-down environment where the real form lives, and one import in the wrong
place turns an optional feature into a mandatory one without anybody noticing.

Inside the fence, two more seams:

- **`transport.py` is the only module in AIrForms that opens a socket.**
  Everything above it builds a request dict and reads a response dict.
  `UrllibTransport` is the default and provider-neutral; `SdkTransport` /
  `OpenAiSdkTransport` are used automatically *only* when the matching
  first-party package is installed and matches the provider in use;
  `RecordedTransport` replays a fixture and **raises on a request it has never
  seen**, which is how a non-deterministic feature sits inside a deterministic
  suite — a changed prompt fails loudly instead of quietly reaching for the
  network.
- **`providers.py` is the only module that knows a service's name.** Per
  provider: the key variable, base URL, path, per-task default models, headers,
  request shape, reply shape. The prompt, the validation gate and the commands
  are the same code for both. Adding the second provider changed them not at
  all.

The provider is **inferred, not demanded**, and `fillerai llm status` prints
which step decided it: explicit `--provider`, then `FILLERAI_LLM_PROVIDER`,
then a model name that gives itself away, then exactly one of
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` being set, then what the neutral
`FILLERAI_LLM_KEY` starts with, then Anthropic.

**The validation gate is the feature, not the asking.** A proposed rule is
disbelieved until it survives, in cost order: this project's own spec parser;
every name in `follows` being a real field and not the target; every producible
value being one the target can actually hold; the whole set being acyclic
(checked here rather than left to `_resolution_order`, which tolerates a cycle
by leaving fields in place — correct for a generator, useless as a verdict);
and finally 200 generated records through `validate` and `coherence_report`,
compared against the same records generated without the rules, with a rule
blamed for the problems quoting its field name and dropped. `propose` returns
proposals and `apply` is a **separate command** — the reviewer in the middle is
the feature.

**The bot's reader** (`llm/understand.py`) is the second use, and it follows
the same pattern: the model is asked for exactly what the local reader
produces, every value goes through the same `accept()` a typed value does, and
any failure is a turn read locally. It is turned on by `serve --bot-llm`, not
by a key being present, because it sends what end users type rather than
field names.

**Transcription** (`llm/transcribe.py`) is the third, for the chat's
microphone where the browser's own speech service is blocked. The page
records the phrase and the server sends it to OpenAI's transcription endpoint
with an OpenAI key (never an Anthropic one; that API takes no audio). The
upload is `transport.post_form`, so the socket stays in `transport.py`. It is
turned on by `serve --bot-transcribe`, for the same reason as `--bot-llm`.

---

## 9. Invariants

These are the things a change should not quietly break. Most have a test.

1. **No required runtime dependencies.** `dependencies = []` in
   `pyproject.toml`, asserted by `tests/test_llm_fence.py`. The one optional
   extra, `fillerai[postgres]` (psycopg), is imported only when a
   `postgresql://` URL is opened — by code inspection; no test asserts that.
2. **No build step.** The UI is files on disk, served as they are.
3. **Nothing in the core opens a socket to the outside.** The one exception
   is `llm/transport.py`, behind the import fence. Beyond the server's own
   listening port, the only other connections are on the machine or chosen
   by whoever runs it: the `/sample/` proxy to `127.0.0.1`, the sample
   application calling AIrForms' `/v1`, and the Postgres server named by a
   `postgresql://` URL.
4. **Nothing leaves the machine unless somebody set a key and ran an `llm`
   command**, or set a key and started the server with `--bot-llm` or
   `--bot-transcribe` (or their variables, `FILLERAI_BOT_LLM` /
   `FILLERAI_BOT_TRANSCRIBE`), or pointed it at a Postgres server elsewhere.
5. **The schema is the only cross-stage contract**, and its version rules hold
   (§2).
6. **A shipped migration is never edited** (§5).
7. **The confidence number means the same thing across engines**, because the
   holdout and the calibration are shared, not per-engine.
8. **Confidence is never fitted on the rows it describes** — and the combiner
   is not fitted on the rows the calibration uses either.
9. **`/v1` never reads the session cookie**, and `/api` never accepts a bearer
   token.
10. **An API key is never written to disk** by the UI, nor printed by any
    command (`Settings.redacted` shows four characters).
11. **A model's proposed rule is never believed without the gate**, whoever
    proposed it.
12. **The bot service keeps no conversation.** A turn is a pure function of
    the input, the state the client sent back and the templates; the state is
    re-checked on every turn because it has been through a browser.
13. **Real records come out in the generated shape.** Whatever `realdata`
    cleans has one key per fillable field, written the way the generator
    writes it, so no stage after it needs to know where a dataset came from.
    Pinned by `tests/test_realdata.py`, which round-trips every example's
    generated records through CSV, JSON and NDJSON and back unchanged.

---

## 10. Where things live

```
fillerai/
  schema.py            the versioned field-schema contract
  infer.py             semantic type from ranked evidence
  cli.py               every command
  realdata.py          real records: read, map onto a form, clean
  store.py             the library in a directory
  db.py                the database: the interface, SQLite and Postgres
  dbstore.py           the same library in the database, with an owner
  auth.py              users, passwords, roles and sessions
  tokens.py            bearer credentials for an application
  extract/             dom.py, html_form.py, spec.py
  generate/            catalogs.py, persona.py, render.py, dataset.py
  train/               features, associate, derive, model, evaluate,
                       trace, script, algos/
  simulate/            form.py, effort.py, run.py
  bot/                 template, understand, conversation, starters/
                       - Bot Builder, see bot-builder.md
  llm/                 providers, config, transport, client, prompts,
                       cost, rules, understand, transcribe (behind the
                       import fence)
  web/
    server.py          the UI's /api, the pages, the /sample/ proxy, and
                       the routing for both surfaces
    rest.py            the /v1 integration API
    botrest.py         the bot service on /v1
    keyring.py         API keys typed into the UI, in memory only
    docs.py            /docs: the Markdown renderer, and the help panel's
                       sections
    guide.md           the user guide, for /docs and the help panel
    static/            index.html, app.js, styles.css, login.*,
                       product.*, docs-gate.js, docs.css
    static/client/     fillerai.js, demo.html, chat.html,
                       fillerai-chat.css - the browser client
  sampleapp/           the sample application serve starts next to the UI;
                       imports nothing from AIrForms, reaches it over /v1
examples/              one HTML form and four field specs (sample output
                       in out/)
tests/                 941 tests, offline, no dependencies
Dockerfile,            the Railway deploy; see deploy-railway.md
  railway.json
livetests/             the LLM acceptance gate; needs a key and an opt-in
docs/                  this directory
```

---

## See also

- [process.md](process.md) — the same system as a sequence of things you do.
- [assumptions.md](assumptions.md) — what all of this takes as given.
- [pending.md](pending.md) — what is known to be missing or wrong.
- [integration.md](integration.md) — the `/v1` API and the browser client.
- [real-data.md](real-data.md) — reading, mapping and cleaning real records.
- [deploy-railway.md](deploy-railway.md) — the hosted deployment.
- [training-and-scale.md](training-and-scale.md) — what a run costs, and
  behaviour at 20,000 records.
