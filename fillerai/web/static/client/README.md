# FillerAI JavaScript client

One file, no dependencies, no build step. It talks to the `/v1` integration
API of a running FillerAI server and asks a trained model what to put in a
form.

Full documentation, including the REST endpoints it wraps and how to get an
API token: [`docs/integration.md`](../../../../docs/integration.md).

## Using it

The server serves this file at `/client/fillerai.js`, so the simplest thing
is to point at the service you are going to talk to:

```html
<script type="module">
  import { FillerAI } from "http://localhost:8000/client/fillerai.js";

  const filler = new FillerAI({
    baseUrl: "http://localhost:8000",
    token: "flr_...",
  });
  const { models } = await filler.models();
  const answer = await filler.suggest(models[0].id, { policy_number: "A-1188" });
  console.log(answer.values);
</script>
```

For a bundled application, copy `fillerai.js` into your source tree and
import it like any other source file. It is one ES module with no imports of
its own, so a bundler needs no help with it, and there is nothing to install.

## What is in it

- `FillerAI` — `health()`, `models()`, `model(id)`, `suggest(id, observed)`,
  `fill(id, observed)`, `batch(id, records)`, `bind(form, id)`.
- `FormBinder` — a model bound to a real `<form>`: fills as you type, never
  overwrites what a person typed, and marks what it filled.
- `FillerAIError` — carries `status`, `code` and `isAuth`.

## Trying it

`demo.html` in this folder is a working page that connects, lists the models
in your library, draws a form from one of them and fills it in as you type,
with each value's confidence and reason underneath it. A running server
serves it at `/client/`.
