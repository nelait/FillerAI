# Glossary

The words these documents and the code use with a specific meaning. Where a
word names a thing in the code, the module is given.

| Term | Meaning |
|---|---|
| **action** | A button the bot offers at the end of a turn, identified by an id the chat sends back as `input` of type `action`. The two finishing actions a template can offer are `fill_form` and `submit`. See [bot-builder.md](bot-builder.md) §3. |
| **alias** | Another word a person uses for a template field ("zip" for `postal_code`). The label and defaults for the semantic type are always aliases too. `fillerai/bot/template.py`. |
| **algorithm / engine** | How the middle layer of a model is learned. Six are registered: `statistical` (the default), `tree`, `forest`, `nearest`, `bayes`, `linear`. `fillerai/train/algos/`. |
| **API token** | The credential an application uses on `/v1`: `flr_<id>_<secret>`, shown once, stored as a SHA-256 hash, acting as the user who issued it. `fillerai/tokens.py`. |
| **calibration** | Mapping a model's raw score to the observed rate of being right, measured on held-out rows, so that a confidence of 0.9 is right about 90% of the time whichever engine produced it. |
| **cleaning fix** | One of the eight named, switchable steps that clean real records: `trim`, `blanks`, `case`, `options`, `types`, `invalid`, `duplicates`, `incomplete`. `invalid` and `incomplete` throw information away, so they are off by default. Each reports what it changed, or would have. `FIXES` in `fillerai/realdata.py`; see [real-data.md](real-data.md). |
| **coherence** | Whether the values in one generated record belong together (the ZIP is in the city, the area code matches the state). `coherence_report()` in `fillerai/generate/dataset.py`. |
| **column mapping** | Which field of the form each column of an uploaded file of real records fills: guessed by name or label, checked by a person, and `COLUMN=FIELD` on the command line. `suggest_mapping()` in `fillerai/realdata.py`. |
| **context.current** | What the host application already holds for the person (their address on file), sent with a bot turn so the reply can show before and after. |
| **CSRF token** | The per-session value the UI echoes in the `X-FillerAI-Token` header on every `/api` POST. |
| **Data step** | Step 3 of the UI (it was called Generate until 0.19.0): **Generate sample records** or **Upload real records**. Its panel id is still `generate`. |
| **dataset** | A set of records for one schema, generated or real (a real one carries `origin: real` in its metadata). Library kind `dataset`, ids `dat-`. |
| **decline** | A model's refusal to suggest a value for a field it cannot predict reliably. A declined field is left for the agent rather than guessed. |
| **declared rule** | A relationship written into a field spec with `follows` (this field depends on those) and `when` (its value given theirs). The generator honours it; the LLM feature proposes them. See [architecture.md](architecture.md) §2. |
| **docs access code** | The code an administrator sets in Settings that opens `/docs` to a browser. Stored as a scrypt hash; the docs cookie is an HMAC keyed by it, so changing the code closes the docs to every browser that had the old one. Closed until a code is set. `fillerai/web/server.py`, `fillerai/web/docs.py`. |
| **effect** | What a bot reply tells the host application to do: `fill_form` or `submit`. The only part of a reply the host must act on. |
| **effort constants** | The five numbers in `fillerai/simulate/effort.py` (seconds per keystroke, per field, per check, and so on) that turn a simulation into time saved. None has been measured; see [assumptions.md](assumptions.md) §4. |
| **entry** | One item in the library, whose contents are never rewritten (a saved template is a new entry replacing the old one), with an id, a kind, an owner, a parent and a payload. |
| **evidence / seed fields** | The fields an agent types first, from which the model fills the rest. `suggest_seed_fields()` picks the most informative ones. |
| **extract** | The first stage: read HTML or a field spec into a schema. `fillerai/extract/`. |
| **field spec** | A short JSON description of a form written by hand, the alternative to HTML markup as input to `extract`. It is where declared rules are written. `fillerai/extract/spec.py`. |
| **floor** | The last layer of a model: plain value frequencies, used when neither a rule nor the engine has anything better. |
| **follows** | In a field spec, the fields a declared rule depends on. In a bot template, the fields whose change makes the value on file for this one outdated (a new city makes the old ZIP wrong). |
| **gate / validation gate** | The checks every rule a language model proposes must survive before it is offered for review: the spec parser, real field names, producible values, no cycles, then 200 generated records compared with and without the rule. `fillerai/llm/rules.py`. |
| **headroom** | The part of a form a better model could still fill. Measured at 6.8% of cells in [llm-modelling.md](llm-modelling.md). |
| **help panel** | The **Help** tab on the right edge of every screen of the app (or the `?` key): the how-to for the screen showing, cut from the user guide `fillerai/web/guide.md` and served by `/api/help`. |
| **host application** | Somebody else's application that puts the AIrForms chat window or form binding in front of its users. |
| **import fence** | The rule, enforced by `tests/test_llm_fence.py`, that nothing outside `fillerai/llm/` imports that package at module scope, so no network-capable code loads unless a feature that needs it runs. |
| **keyring** | Where API keys typed into Settings are held: server memory, per user, never written to disk. `fillerai/web/keyring.py`. |
| **library** | Everything the stages produce, with lineage. Two implementations of one interface: `Store` (a directory, the CLI's default) and `DatabaseStore` (SQLite, or Postgres with the optional extra; owned entries; the server's). |
| **lineage** | The chain of parents from a model back to the dataset, schema and source it came from. |
| **model** | A trained `AutofillModel`: rules, then the engine, then the floor, with calibrated confidence per field. Library kind `model`, ids `mdl-`. `fillerai/train/model.py`. |
| **next-step bar** | The bar at the end of each UI stage, where its result is, that leads to the next one (**Go to Train**, **Simulate with this model**). The **trail** above the panel names the loaded form, records and model, each a link back to its stage. |
| **--no-auth** | Running the server without accounts, for one person on their own machine. Refused on any address but loopback. |
| **owner** | The user a library entry belongs to. Nobody can reach another owner's entries. |
| **Postgres backend** | `PostgresDatabase` in `fillerai/db.py`, through psycopg 3, installed only with `pip install 'fillerai[postgres]'` and chosen by a `postgresql://` URL. The one optional dependency; SQLite needs nothing. |
| **persona** | One invented person from which a whole coherent record is generated. `fillerai/generate/persona.py`. |
| **product page** | The public page at `/`, with a **Sign in** link. The app itself is at `/app`. |
| **reader** | The part of the bot that turns a phrase into a template choice and values. Local by default (`fillerai/bot/understand.py`); a language model with `--bot-llm` (`fillerai/llm/understand.py`). |
| **real records** | Past submissions, as opposed to generated records: read from a CSV, TSV, JSON or NDJSON export, mapped onto the form, cleaned, and saved as a dataset marked real. `fillerai/realdata.py`, `fillerai clean`; see [real-data.md](real-data.md). |
| **rule (in a model)** | A deterministic relationship found in the data during training (a field that always equals another, or is fixed by it), applied before the engine. `fillerai/train/derive.py`. |
| **sample application** | Northwind Mutual, a demo customer portal in `fillerai/sampleapp/` that uses AIrForms only over `/v1`. Started by `serve` on port 8100, and also reached at `/sample/` on AIrForms' own port, signed in only unless `--sample-public`. It has a form per bot template and shows one at a time, opened by the chat or from its menu. |
| **schema** | The versioned `FormSchema`: fields, screens, types and semantic types. The one contract between stages. Library kind `schema`, ids `sch-`. `fillerai/schema.py`. |
| **script** | The generated, standalone Python script that reproduces a training run, kept in the library. Kind `script`, ids `scr-`. `fillerai/train/script.py`. |
| **semantic type** | What a field means rather than how it is typed: `postal_code`, `email`, `policy_number` and so on. `SEMANTIC_TYPES` in `fillerai/schema.py`; inferred by `fillerai/infer.py`. |
| **simulate** | Playing a model against forms it has not seen and costing the result. `fillerai/simulate/`. |
| **source** | The markup or spec a schema was extracted from, kept in the library. Kind `source`, ids `src-`. |
| **starter** | A template shipped with the package (`address_change`, `document_request`) in `fillerai/bot/starters/`. |
| **state** | The conversation a bot turn hands back and the client sends with the next turn. The service keeps nothing between turns and re-checks `state` every time. |
| **stale_action** | The `409` answer to an action id that was not offered on the previous turn, which is what makes a double click submit once. |
| **template** | What a bot request looks like: a key, example phrases, fields and actions. Library kind `template`, ids `tpl-`. `fillerai/bot/template.py`. |
| **Test on real records** | The Simulate step's card that scores a loaded model on any dataset (fill from the seed fields, per-field accuracy, time saved) and warns when it is the model's own training data. `POST /api/evaluate`. |
| **--trust-proxy** | Believing the visitor's address and scheme from the `X-Forwarded-For`/`-Proto` headers of a hosting platform's proxy (or `FILLERAI_TRUST_PROXY=1`). Only behind such a proxy; see [deploy-railway.md](deploy-railway.md). |
| **turn** | One `POST /v1/bot/turn` (or `/api/bot/turn` from the Bots tab): an input and state in, a reply and new state out. |
| **`/api` and `/v1`** | The UI's own endpoints (cookie and CSRF, may change) and the stable service for other applications (bearer token, versioned). See [architecture.md](architecture.md) §7. |
