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
| **coherence** | Whether the values in one generated record belong together (the ZIP is in the city, the area code matches the state). `coherence_report()` in `fillerai/generate/dataset.py`. |
| **context.current** | What the host application already holds for the person (their address on file), sent with a bot turn so the reply can show before and after. |
| **CSRF token** | The per-session value the UI echoes in the `X-FillerAI-Token` header on every `/api` POST. |
| **dataset** | A set of records for one schema, generated or real. Library kind `dataset`, ids `dat-`. |
| **decline** | A model's refusal to suggest a value for a field it cannot predict reliably. A declined field is left for the agent rather than guessed. |
| **declared rule** | A relationship written into a field spec with `follows` (this field depends on those) and `when` (its value given theirs). The generator honours it; the LLM feature proposes them. See [architecture.md](architecture.md) §2. |
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
| **host application** | Somebody else's application that puts the AIrForms chat window or form binding in front of its users. |
| **import fence** | The rule, enforced by `tests/test_llm_fence.py`, that nothing outside `fillerai/llm/` imports that package at module scope, so no network-capable code loads unless a feature that needs it runs. |
| **keyring** | Where API keys typed into Settings are held: server memory, per user, never written to disk. `fillerai/web/keyring.py`. |
| **library** | Everything the stages produce, with lineage. Two implementations of one interface: `Store` (a directory, the CLI's default) and `DatabaseStore` (SQLite, owned entries, the server's). |
| **lineage** | The chain of parents from a model back to the dataset, schema and source it came from. |
| **model** | A trained `AutofillModel`: rules, then the engine, then the floor, with calibrated confidence per field. Library kind `model`, ids `mdl-`. `fillerai/train/model.py`. |
| **--no-auth** | Running the server without accounts, for one person on their own machine. Refused on any address but loopback. |
| **owner** | The user a library entry belongs to. Nobody can reach another owner's entries. |
| **persona** | One invented person from which a whole coherent record is generated. `fillerai/generate/persona.py`. |
| **reader** | The part of the bot that turns a phrase into a template choice and values. Local by default (`fillerai/bot/understand.py`); a language model with `--bot-llm` (`fillerai/llm/understand.py`). |
| **rule (in a model)** | A deterministic relationship found in the data during training (a field that always equals another, or is fixed by it), applied before the engine. `fillerai/train/derive.py`. |
| **sample application** | Northwind Mutual, a demo customer portal in `fillerai/sampleapp/` that uses AIrForms only over `/v1`. Started by `serve` on port 8100. It has a form per bot template and shows one at a time, opened by the chat or from its menu. |
| **schema** | The versioned `FormSchema`: fields, screens, types and semantic types. The one contract between stages. Library kind `schema`, ids `sch-`. `fillerai/schema.py`. |
| **script** | The generated, standalone Python script that reproduces a training run, kept in the library. Kind `script`, ids `scr-`. `fillerai/train/script.py`. |
| **semantic type** | What a field means rather than how it is typed: `postal_code`, `email`, `policy_number` and so on. `SEMANTIC_TYPES` in `fillerai/schema.py`; inferred by `fillerai/infer.py`. |
| **simulate** | Playing a model against forms it has not seen and costing the result. `fillerai/simulate/`. |
| **source** | The markup or spec a schema was extracted from, kept in the library. Kind `source`, ids `src-`. |
| **starter** | A template shipped with the package (`address_change`, `document_request`) in `fillerai/bot/starters/`. |
| **state** | The conversation a bot turn hands back and the client sends with the next turn. The service keeps nothing between turns and re-checks `state` every time. |
| **stale_action** | The `409` answer to an action id that was not offered on the previous turn, which is what makes a double click submit once. |
| **template** | What a bot request looks like: a key, example phrases, fields and actions. Library kind `template`, ids `tpl-`. `fillerai/bot/template.py`. |
| **turn** | One `POST /v1/bot/turn` (or `/api/bot/turn` from the Bots tab): an input and state in, a reply and new state out. |
| **`/api` and `/v1`** | The UI's own endpoints (cookie and CSRF, may change) and the stable service for other applications (bearer token, versioned). See [architecture.md](architecture.md) §7. |
