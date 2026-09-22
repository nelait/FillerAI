# Calling FillerAI from another application

FillerAI's UI is where a model is built. This is how something else uses
one: a REST service at `/v1`, and a JavaScript client for the browser
applications that are the common case.

The short version:

```bash
fillerai tokens add krishna --name "claims desk"     # issue a credential
fillerai serve                                        # the service is at /v1
```

```js
import { FillerAI } from "http://localhost:8000/client/fillerai.js";

const filler = new FillerAI({ baseUrl: "http://localhost:8000", token: "flr_..." });
const { models } = await filler.models();
const answer = await filler.suggest(models[0].id, { policy_number: "A-1188" });

answer.values;      // { coverage_type: "Comprehensive", ... } - put these in the boxes
answer.suggestions; // every field, with its confidence and its reason
```

There is a working page at `http://localhost:8000/client/` that does exactly
this against your own library, and its source is
[`fillerai/web/static/client/demo.html`](../fillerai/web/static/client/demo.html).

---

## Why this is a second surface

Everything under `/api` is the FillerAI UI talking to its own server, and it
is shaped for that: a session cookie, a CSRF header, one POST per button, and
a model held in memory under a handle the browser was handed a moment ago.
None of that suits a claims system calling from another machine next Tuesday.

So `/v1` makes different promises:

| | `/api` (the UI) | `/v1` (integrations) |
|---|---|---|
| Credential | session cookie + CSRF header | `Authorization: Bearer flr_...` |
| Addressing | in-memory handle from this session | library id, `mdl-...`, permanent |
| Methods | POST everything | GET to read, POST to ask |
| Stability | changes with the UI | changes only with the version number |
| Cross-origin | never | yes, with a token |

**The cookie is not read by `/v1` at all.** That is the point of the second
credential: a surface that honoured the cookie could be driven by any page
open in the user's browser.

## Authenticating

An API token belongs to an account and reads that account's library. Issue
one from the command line:

```bash
fillerai tokens add krishna --name "claims desk"
fillerai tokens add krishna --name "kiosk" --days 90 --model mdl-2026...
fillerai tokens list
fillerai tokens revoke "claims desk"
```

or from **Settings → API tokens** in the UI, which is the same thing with a
Copy button.

Three things worth knowing:

- **It is shown once.** The secret is stored as a hash, so a lost token is
  reissued, never recovered.
- **It can be pinned to one model** (`--model`), which is the right shape for
  an application embedded in a single form: the token then cannot see or ask
  about anything else in that library.
- **It follows the account.** Disabling the account revokes its tokens;
  deleting the account deletes them.

Send it as a bearer token:

```bash
curl -H "Authorization: Bearer flr_..." http://localhost:8000/v1/models
```

### Running without accounts

`fillerai serve --no-auth` is the single-user tool: no login, one library, and
`/v1` answers without a token, because everything else already does. That is
only allowed on loopback, so the thing keeping other people out is that they
cannot reach the port.

## Cross-origin requests

A React application on `localhost:5173` calling a service on `localhost:8000`
is a cross-origin request, so the browser asks first.

- **With accounts on, any origin may call `/v1`.** This is safe, and it is
  safe for one specific reason: the credential is a bearer token the calling
  page had to be *given*, not a cookie the browser attaches by itself. A page
  without a token gets a 401 whatever its origin. Credentials are never
  allowed on these responses, which is what keeps that true.
- **With `--no-auth`, no origin may, until you name one.** There is no
  credential in that mode, so allowing any origin would let any page you have
  open read your library:

  ```bash
  fillerai serve --no-auth --cors-origin http://localhost:5173
  ```

`--cors-origin` is repeatable, and takes `*` if you mean it.

## The endpoints

All of them answer JSON. A refusal is
`{"error": "a sentence", "code": "a_short_code"}` — branch on the code, show
the sentence.

### `GET /v1/health`

No token needed. Is this a FillerAI, and which version.

### `GET /v1/models`

Every model this token may ask about.

```json
{"count": 1, "models": [{
  "id": "mdl-20260922-021500-3f9a",
  "name": "auto_insurance_quote: forest",
  "form": "auto_insurance_quote",
  "algorithm": "forest",
  "trained_on": 900,
  "rules": 6,
  "created": "2026-09-22T02:15:00+00:00"
}]}
```

### `GET /v1/models/{id}`

One model in full — enough to draw a form as well as fill one.

```json
{
  "model": { "id": "...", "form": "auto_insurance_quote", "fields": 42, ... },
  "threshold": 0.7,
  "ask_first": ["driver_postal_code", "vehicle_year", "policy_start_date"],
  "screens": [{"id": "driver", "title": "Driver"}, ...],
  "fields": [{
    "name": "coverage_type", "label": "Coverage", "semantic_type": "unknown",
    "screen": "coverage", "group": "coverage", "control": "select",
    "options": [{"value": "Comprehensive", "label": "Comprehensive"}, ...],
    "required": true, "read_only": false,
    "how": "learned", "from": ["vehicle_year"], "strength": 0.82,
    "kind": "enumerable", "format": null, "fill_rate": 0.98
  }, ...]
}
```

`ask_first` is the model's own answer to *what should I ask a person for
before anything else* — the few fields that unlock the most. `how` says where
each field's answer comes from: `rule`, `learned`, `usual`, or `you` for the
ones nobody can predict.

### `POST /v1/models/{id}/suggest`

The main call. What goes in the boxes that are still empty.

```json
{"observed": {"vehicle_year": "2019"}, "threshold": 0.7, "fields": ["coverage_type"]}
```

`threshold` and `fields` are optional; without a threshold the service uses
the one the model was calibrated against.

```json
{
  "model_id": "mdl-...", "threshold": 0.7,
  "given": {"vehicle_year": "2019"},
  "values": {"coverage_type": "Comprehensive"},
  "offered": 1, "considered": 40,
  "suggestions": [{
    "field": "coverage_type", "value": "Comprehensive",
    "confidence": 0.91, "score": 0.88, "basis": "learned",
    "because": ["vehicle_year 2019 -> Comprehensive in 88% of records"],
    "alternatives": [["Third party", 0.09]],
    "accepted": true
  }, ...]
}
```

- `values` is the answer for a client that just wants to fill the form.
- `suggestions` is every field the model considered, including the ones it
  declined, each with the reason — which is what an agent-facing form needs
  to show before somebody accepts a hundred values they did not type.
- `accepted` is `confidence >= threshold`, worked out once here rather than
  in every client.

**An unknown field name is refused, not ignored.** Quietly dropping one is
how an integration ends up believing it has been sending the policy number
for a month.

### `POST /v1/models/{id}/fill`

The same work, for a caller that wants the finished record rather than the
reasoning.

```json
{"record": {"vehicle_year": "2019", "coverage_type": "Comprehensive", ...},
 "filled": ["coverage_type", ...], "offered": 12, "threshold": 0.7}
```

### `POST /v1/models/{id}/batch`

`fill` over up to 500 partial records in one round trip — a night's queue,
one HTTP call and one model load.

```json
{"records": [{"vehicle_year": "2019"}, {"vehicle_year": "2004"}]}
```

## The JavaScript client

One file, no dependencies, no build step:
[`fillerai/web/static/client/fillerai.js`](../fillerai/web/static/client/fillerai.js). The server serves it at
`/client/fillerai.js`, so an application can fetch it from the service it is
about to talk to rather than vendoring a copy that drifts.

Three ways to use it, all the same file:

```js
// A bundled application: copy fillerai.js into src/ and import it like any
// other source file. A bundler needs no help with it - it is one ES module
// with no imports of its own.
import { FillerAI } from "./fillerai.js";

// A plain page, straight from the running service.
// <script type="module">import { FillerAI } from "/client/fillerai.js";</script>

// A page that also has classic scripts: loading the module puts
// window.FillerAI in place on the way past.
```

There is no npm package and no build step, on purpose. The stack rule for
this project is no dependencies and nothing to install, and it applies to the
client as much as to the server; an npm package would mean a registry, a
publish step and a version to keep in step with the server's, to save one
`curl`. The cost is that you copy a file or point at a URL instead of typing
`npm install`. The file is 400 lines and has no transitive anything, and it
ships inside the Python package, so the copy at `/client/fillerai.js` is
always the one that matches the service answering you.

### Binding it to a real form

`bind()` is the part every integration would otherwise write for itself:

```js
const binder = await filler.bind(document.querySelector("form"), modelId, {
  threshold: 0.7,
  onSuggest: (answer) => showReasons(answer.suggestions),
});
```

It watches the fields a person types into, asks after a pause rather than per
keystroke, fills what the model is confident about, and follows three rules:

- **What the person typed wins.** A field they have touched is never
  overwritten, and is sent to the model as something it now knows.
- **Editing a suggestion takes it back.** From that point the field is
  theirs.
- **A suggestion is marked as one** — a `fillerai-filled` class and
  `data-fillerai="suggested"` — so the page can show which values came from
  the model.

`binder.reset()` clears both, `binder.stop()` unhooks it.

### React

No hook is shipped, because a hook that imports React would be a dependency.
The client is plain enough not to need one:

```jsx
import { useEffect, useMemo, useState } from "react";
import { FillerAI } from "./fillerai.js";

function useAutofill(modelId, observed) {
  const filler = useMemo(
    () => new FillerAI({ baseUrl: BASE, token: TOKEN }), []);
  const [suggested, setSuggested] = useState({});

  useEffect(() => {
    let live = true;
    const timer = setTimeout(async () => {
      const answer = await filler.suggest(modelId, observed);
      if (live) setSuggested(answer.values);
    }, 250);
    return () => { live = false; clearTimeout(timer); };
  }, [filler, modelId, JSON.stringify(observed)]);

  return suggested;
}
```

Then render each input with `value={typed[name] ?? suggested[name] ?? ""}`,
and move a field into `typed` the moment somebody edits it. That is the same
three rules as `bind()`, in React's own idiom.

## Errors

| Status | `code` | What happened |
|---|---|---|
| 401 | `no_token` | No `Authorization: Bearer` header |
| 401 | `bad_token` | Not a token, revoked, or expired |
| 403 | `out_of_scope` | The token is pinned to a different model |
| 403 | `account_off` | The token's account was disabled |
| 404 | `not_found` | No such model in this library |
| 400 | `not_a_model` | That id is a schema or a dataset |
| 400 | `unknown_field` | A field name the model has never seen |
| 400 | `bad_threshold` | Not a number between 0 and 1 |
| 405 | `method_not_allowed` | Right path, wrong verb |
| 413 | `too_large` | Over 500 records, or an 8MB body |

`FillerAIError` in the JavaScript client carries `status` and `code`, and
`error.isAuth` is true for the first four.

## Notes

- **Models are cached in the server between calls** — an integration asks the
  same one repeatedly, and re-reading a few hundred kilobytes of JSON each
  time would make that unusable. Library entries are immutable once written,
  so a cached model cannot go stale; deleting one evicts it.
- **Nothing here trains.** `/v1` is read-only against the library: it asks
  models questions and never writes. Building and training stay in the UI and
  the CLI, where somebody is watching.
- **The token has no rate limit.** This is a tool on somebody's machine or
  their network, not a public service. If that changes, that is where to
  start.
