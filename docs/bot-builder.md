# Bot Builder

A template says what a request looks like — "change my address" is a street,
a unit, a city, a state, a ZIP and a country; "send me a document" is a policy
number, a document type and a name. Somebody else's application puts a chat
window in front of its users, and whatever they type, say or click goes to the
bot service, which works out which template they mean, fills in what they
said, and answers with the values, what changed against what the application
already holds, and what the person can do next.

This page is the contract between the chat window and the bot service. It was
written before the code on purpose: the chat window is in someone else's
product, so the interface is the part that cannot be changed casually later.
Everything below is checked against the code at version 0.14.1, and each
example reply is what the service actually returns.

---

## 1. The three parts

```
  somebody else's app                                FillerAI
 +-----------------------------------+        +-------------------------+
 |  their own form     chat window   |        |  bot service  /v1/bot   |
 |  +-------------+   +------------+ |  turn  |  +-------------------+  |
 |  | Street  ... |<--| effect     | |------->|  | which template?   |  |
 |  | City    ... |   | messages   | |        |  | which values?     |  |
 |  | ZIP     ... |   | actions    | |<-------|  | what is missing?  |  |
 |  +-------------+   | [mic] [__] | | reply  |  +-------------------+  |
 |                    +------------+ |        |   templates (library)   |
 +-----------------------------------+        |   autofill model (opt.) |
                                              +-------------------------+
```

- **Templates** are authored in FillerAI (the Bots tab, the `/v1/templates`
  endpoints, or `fillerai bot add`) and kept in the library like everything
  else, per owner.
- **The bot service** is `POST /v1/bot/turn`. It holds no conversation in
  memory; everything a conversation needs is in the `state` it hands back (§4).
- **The chat window** is `BotChat` / `ChatWidget` in the existing JavaScript
  client, `fillerai.js`. It owns the microphone, the message list and the
  action buttons, and hands the host application every `effect` it must act on.

The service never writes to the host's systems. It says *what* to prefill or
submit; the host does it, and may tell the service how it went (§3.3).

---

## 2. A template

```json
{
  "template_version": "1.0",
  "key": "address_change",
  "name": "Address change",
  "description": "Update the mailing address on file",
  "examples": ["update my address", "I moved", "change my city to Austin"],
  "fields": [
    {"name": "street_address", "label": "Street address",
     "semantic_type": "street_address", "required": true,
     "aliases": ["house no", "house number", "street"]},
    {"name": "unit", "label": "Unit", "semantic_type": "address_line2",
     "aliases": ["apt", "apartment", "suite"], "follows": ["street_address"]},
    {"name": "city", "label": "City", "semantic_type": "city", "required": true},
    {"name": "state", "label": "State", "semantic_type": "state", "required": true,
     "follows": ["city"]},
    {"name": "postal_code", "label": "ZIP code", "semantic_type": "postal_code",
     "required": true, "aliases": ["zip", "zipcode", "postcode"],
     "follows": ["street_address", "city", "state"]},
    {"name": "country", "label": "Country", "semantic_type": "country",
     "options": ["US", "CA", "MX"]}
  ],
  "actions": ["fill_form", "submit"],
  "model_id": null,
  "done_message": "Your address change is in."
}
```

| Key | Meaning |
|---|---|
| `key` | **The stable name.** The host branches on it, and saving a template with the same key replaces the old one. Lower case, digits and `_`. |
| `examples` | Things a person might say to mean this. They are what intent recognition is matched against, so a few real phrasings beat a long description. |
| `fields[].name` | The join key, exactly as with the form schema: it is the name of the field in the host's form, in `context.current`, in `values`, and in a linked model. |
| `fields[].semantic_type` | One of the schema's semantic types. It decides how a value is recognised without a label ("92618" is a ZIP) and how it is tidied ("ca" is `CA`). |
| `fields[].aliases` | Other words a person uses for the field. The label and a set of defaults for the semantic type are always included. |
| `fields[].options` | A closed list. A value off the list is not accepted, and a field with a short list is offered as buttons. |
| `fields[].follows` | Fields this one depends on. When one of them changes in the conversation, the value on file for this one no longer holds and is asked for instead of carried over: a new city makes the old ZIP code wrong. The word is the one field specs already use for a declared rule. |
| `fields[].example` | Shown after a typed question ("For example 92618"), not after a spoken one. |
| `actions` | Which of the two finishing actions this template offers: `fill_form`, `submit`, or both. |
| `model_id` | Optional: a trained FillerAI model whose fields share names with this template. After what the person said is filled in, the model is asked to complete related fields (a new city → its state), at its calibrated threshold. |

A template can be started from a form schema already in the library (every
field of the schema, with its label, semantic type, options and required flag,
leaving out passwords, free text and read-only fields) and then cut down,
which is how an existing FillerAI form becomes a bot. Two starters ship inside
the package, `address_change` and `document_request`
(`fillerai/bot/starters/`), and are added to a library from the Bots tab or
with `fillerai bot add --starter NAME`.

Templates are kept in the library as a sixth kind, `template` (ids `tpl-…`),
per owner like everything else. A template started from a schema records it
as its parent.

---

## 3. One turn

Typing, speaking and clicking a suggested action all arrive the same way: as
one `input` in the same request. That is the decision point 3 of the ask is
about, and it is why there is one endpoint rather than three.

### 3.1 The request

```http
POST /v1/bot/turn
Authorization: Bearer flr_...
```

```json
{
  "input": {"type": "text", "text": "please update my city from SFO to Irvine and house no 1429 Silverstein",
            "via": "speech", "alternatives": ["please update my city from SFO to Irvine and house number 1429 Silverstein"]},
  "state": null,
  "context": {
    "current": {"street_address": "55 Market St", "unit": "", "city": "San Francisco",
                "state": "CA", "postal_code": "94105", "country": "US"},
    "templates": ["address_change", "document_request"]
  }
}
```

`input` is one of three shapes, and nothing else:

| `type` | Carries | Comes from |
|---|---|---|
| `text` | `text`; `via` is `"typed"` or `"speech"`; `alternatives` optional | the text box, or the microphone once speech recognition has finished a phrase |
| `action` | `action`: an `id` from the previous reply's `actions` | a click on a suggested action |
| `event` | `event`: `"submitted"`, `"submit_failed"` or `"filled"`; `detail` optional | the host, after it acted on an `effect` |

Speech is turned into text **in the browser** (the Web Speech API, through
`SpeechInput` in the client), so the service never receives audio. `via:
"speech"` changes two things: the reply's text is written to be read aloud
(no tables in the message itself), and a phrase that sounds like an action is
only taken as one when it is unmistakable ("submit" alone, not "submit a
claim for..."). `alternatives` are the recogniser's runners-up; the service
tries them only when the first reading finds nothing.

A **typed or spoken phrase can also be an action**: "submit", "yes, send it",
"fill the form", "start over". The service resolves it against the actions it
offered last turn, so "submit" typed and Submit clicked produce the same
reply. An action id that was not offered last turn is refused (`409
stale_action`) rather than guessed at — a double click on an old button must
not submit twice.

`context.current` is what the host already holds for this person. It is how
the service can say "from San Francisco to Irvine" without having been told
San Francisco; the service never stores it. `context.templates` limits which
templates this chat may use; omitted means all of the token's templates.
`context.template` starts the conversation on one template when the host
already knows (a "Change address" button that opens the chat).

### 3.2 The reply

```json
{
  "conversation": {"id": "cnv-6f1c2a9e0b1d", "turn": 1, "status": "collecting"},
  "intent": {"template": "address_change", "name": "Address change",
             "confidence": 0.74, "how": "local", "changed": true},
  "understood": {"text": "please update my city from SFO to Irvine and house no 1429 Silverstein", "via": "speech"},
  "messages": [{"role": "bot",
                "text": "OK, an address change. Street address to 1429 Silverstein and city to Irvine. What is the new state?"}],
  "form": {
    "template": "address_change",
    "fields": [
      {"name": "street_address", "label": "Street address", "required": true,
       "before": "55 Market St", "after": "1429 Silverstein", "source": "said",
       "confidence": 0.9, "status": "changed"},
      {"name": "unit", "label": "Unit", "required": false, "before": null,
       "after": null, "status": "empty"},
      {"name": "city", "label": "City", "required": true,
       "before": "San Francisco", "after": "Irvine", "source": "said",
       "confidence": 0.95, "status": "changed", "said_before": "SFO"},
      {"name": "state", "label": "State", "required": true,
       "before": "CA", "after": null, "status": "outdated"},
      {"name": "postal_code", "label": "ZIP code", "required": true,
       "before": "94105", "after": null, "status": "outdated"},
      {"name": "country", "label": "Country", "required": false,
       "before": "US", "after": "US", "source": "current", "status": "kept"}
    ],
    "values":  {"street_address": "1429 Silverstein", "city": "Irvine", "country": "US"},
    "changes": {"street_address": {"before": "55 Market St", "after": "1429 Silverstein"},
                "city": {"before": "San Francisco", "after": "Irvine"}},
    "missing": ["state", "postal_code"],
    "complete": false
  },
  "actions": [
    {"id": "fill_form", "type": "fill_form", "label": "Fill the form for manual submission", "style": "primary"},
    {"id": "cancel", "type": "cancel", "label": "Start over"}
  ],
  "expects": {"field": "state", "label": "State", "semantic_type": "state"},
  "effect": null,
  "state": {"v": 1, "...": "opaque; send it back unchanged"}
}
```

**`form` is the before/after the host shows.** Every template field appears
once with `before` (from `context.current`), `after` (what the conversation
has so far), and a `status`:

| `status` | Meaning |
|---|---|
| `changed` | `after` differs from `before` |
| `unchanged` | the person gave a value and it is what the host already has |
| `kept` | nobody mentioned it; `after` is `before`, carried over |
| `outdated` | a field it `follows` changed, so what is on file no longer holds; asked for if required |
| `missing` | required, no value from the person and none on file |
| `empty` | optional, no value at all |

`source` says where `after` came from: `said` (read out of what the person
typed or spoke), `clicked` (an answer button), `model` (the linked autofill
model, with its calibrated `confidence`), or `current` (carried over).
`said_before` is the old value the person mentioned ("from SFO"), kept because
it is worth showing when it does not match what is on file.

`values` is every field that has a value, ready to prefill. `changes` is only
the fields that differ, ready for a confirmation card. `missing` is required
fields with no value anywhere, outdated ones included. The next question is
always the first of them, in the template's field order.

**`actions` are the suggested actions**, in the order to show them:

| `type` | Label | Offered when |
|---|---|---|
| `submit` | "Submit" | the template allows it and nothing required is missing |
| `fill_form` | "Fill the form for manual submission" | the template allows it and something has been changed |
| `answer` | the option itself, e.g. "CA" | the question being asked is a field with six options or fewer |
| `choose` | a template's name | the service could not tell two templates apart |
| `cancel` | "Start over" | a template is in progress |

Each carries an `id` to send back, and `style: "primary"` on the one to
encourage. An `answer` also carries `field` and `value`, and a `choose` carries
`template`, so a host drawing its own buttons does not have to parse ids.

**`effect` is the one field the host must act on.** It is `null` on every turn
except the one where the person chose a finishing action:

```json
{"type": "fill_form", "template": "address_change",
 "values": {"street_address": "1429 Silverstein", "city": "Irvine", "state": "CA", "postal_code": "92618", "country": "US"},
 "changes": {"...": "as in form.changes"}}
```

- `fill_form` — put `values` into the host's own form and let the person
  finish it there. The conversation ends (`status: "handed_off"`).
- `submit` — submit `values` through the host's own path. The conversation
  waits (`status: "submitting"`) until the host reports back.

### 3.3 After an effect

The host tells the service how it went with an `event` input, so the bot can
say so in the chat:

```json
{"input": {"type": "event", "event": "submitted", "detail": {"reference": "CHG-20931"}}, "state": {...}}
```

`submitted` ends the conversation (`status: "done"`, the template's
`done_message` plus the reference if one was given). `submit_failed` with
`detail.message` puts it back to `ready` so the person can try again or fill
the form instead. `filled` is optional, and only confirms a hand-off. After a
`fill_form` hand-off the host may also send `submitted` once the person has
submitted the form by hand, and the chat closes with the same message. A host
that never reports is fine: the conversation simply ends at the effect.

---

## 4. Turns without a session on the server

A conversation continues over turns by the client sending back the `state`
it was last given. The service keeps nothing between calls: no table, no
memory, no timeout. That is the same choice as `simulate/run.py` — a pure
function of what it is given — and for the same reasons: a restart loses
nothing, two server processes cannot disagree, and a test is one call.

What `state` carries: the template key, every value so far with its source,
what the person said the old value was, the field the last question was about
(so a bare "92618" lands in the ZIP), the action ids offered last turn (so a
stale click is refused), the status, and the turn number. It is plain JSON the
client does not need to understand.

**The state is not a credential and is not trusted.** It passes through the
end user's browser, so the service re-checks it on every turn: the template
must exist and be allowed, every field must be the template's, every option
value must be on its list. Anything that fails is refused with `bad_state`.
A person editing it could at most put values in the conversation that they
could have typed anyway — which is why the host validates what it submits
exactly as it would validate the same values posted from its own form.

A partially filled template continues in three ways, all through the same
state:

1. **Answering the question.** `expects` names the field; a reply that is just
   a value fills it.
2. **Saying more.** "and the unit is 4B" adds to what is there; "actually make
   it Tustin" replaces the city, because a newer value for a field wins.
3. **Changing the subject.** A phrase that clearly means another template
   ("I also need my policy documents") is recognised as a switch, and the reply
   says what was set aside. `intent.changed` is `true` on that turn.

---

## 5. How the service understands a phrase

Two ways, and the first is always there.

**On this machine, by default.** Intent is matched against each template's
examples, name and field words, with a score the reply reports as
`intent.confidence`. When two templates score within a small margin, the reply
asks, with a `choose` action for each. Values are read out by finding the
field's words (its label, aliases, and the defaults for its semantic type) and
taking what follows them up to the next field's words: "city from SFO to
Irvine" is `city`, before `SFO`, after `Irvine`. Values with an unmistakable
shape — a ZIP, a state, an email, a phone number, an option from a closed
list — are recognised without a label when exactly one field could hold them.
Nothing leaves the machine.

**With a language model, when turned on.** `fillerai serve --bot-llm` (or
`FILLERAI_BOT_LLM=1`) sends the phrase, the template list and the field list
to the configured provider, and asks for the template and the values in a
fixed JSON shape. The answer goes through the same checks as the state —
known template, known fields, values on their option lists — and anything that
fails falls back to the local reading for that turn; the reply's `intent.how`
says which one answered. This is **off by default and a separate switch from
having a key**, because unlike proposing rules it sends what an end user typed,
which is their data rather than the form's. The code lives in
`fillerai/llm/understand.py`, behind the same import fence as the rest.

---

## 6. The endpoints

All under `/v1`, bearer token, same CORS rules as the rest of `/v1`.

| Method | Path | Does |
|---|---|---|
| `GET` | `/v1/templates` | every template this token may use |
| `GET` | `/v1/templates/{key}` | one template in full |
| `POST` | `/v1/templates` | create or replace one (body: the template) |
| `POST` | `/v1/templates/{key}/delete` | remove one |
| `POST` | `/v1/bot/turn` | one turn (§3) |
| `POST` | `/v1/bot/transcribe` | a recording as text, when the server runs with `--bot-transcribe` (§7.1) |

A token **pinned to a model** may run turns only on templates linked to that
model, and cannot create or delete templates. An unpinned token can do both.

Refusals carry a stable `code` as everywhere in `/v1`: `unknown_template`,
`bad_template`, `bad_input`, `bad_state`, `stale_action`,
`template_not_allowed`, `out_of_scope`, and for transcription
`transcription_off` (409) and `transcription_failed` (502).

**Where the token lives.** A chat window runs in an end user's browser, and a
bearer token in that browser is readable by that user. For a real deployment
the chat window talks to the host's own backend, which adds the token and
forwards to `/v1/bot/turn` — `BotChat` takes a `send` function for exactly
this. The demo page passes the token straight through because it is a demo.

---

## 7. The JavaScript client

Three additions to `fillerai.js`, same file, still no dependencies:

```js
import { FillerAI, BotChat, ChatWidget } from "./fillerai.js";

const filler = new FillerAI({ baseUrl, token });
const chat = new BotChat(filler, {
  current: () => myApp.currentAddress(),        // context.current, read every turn
  templates: ["address_change", "document_request"],
  onEffect: async (effect, chat) => {
    if (effect.type === "fill_form") myApp.prefill(effect.values);
    if (effect.type === "submit") {
      const ref = await myApp.submit(effect.template, effect.values);
      await chat.report("submitted", { reference: ref });
    }
  },
});
new ChatWidget(document.querySelector("#chat"), chat);
```

Through the host's backend instead of a token in the browser:

```js
const chat = new BotChat(null, {
  send: (body) => fetch("/my-app/bot", { method: "POST", body: JSON.stringify(body) })
    .then((r) => r.json()),
});
```

- **`BotChat`** is the conversation: it keeps `state`, sends `say(text)`,
  `click(action)` and `report(event)` through one `send(input)`, and runs them
  **one at a time in order** — unlike `FormBinder`, a chat must never drop a
  message because a newer one arrived. `send` can be replaced to go through the
  host's backend.
- **`SpeechInput`** wraps the browser's speech recognition: interim text while
  the person speaks, the final phrase and its alternatives when they stop.
  `SpeechInput.problem()` names what stops it on this page (no recogniser,
  not a secure origin, Brave), and the widget then greys the microphone out
  and says why when it is pressed. The first press opens the microphone
  directly, so a refusal by the page, the browser or the operating system is
  reported by name; every recogniser error, and hearing nothing at all, comes
  back to the chat as a sentence.
- **`ChatWidget`** draws the messages, the before/after card, the suggested
  actions and the text box with its microphone, and reads a reply aloud when
  the phrase it answers was spoken. It is plain DOM with `fai-` class names;
  `/client/fillerai-chat.css` is a default look to link or leave out. It is
  optional: a React app can use `BotChat` alone and draw its own. Its header
  carries a `title` and a **New chat** button (`resettable`, on by default):
  that stops the microphone, clears the log and the suggestions, and calls
  `chat.reset()`, then the host's `onReset` so it can put its own form back.
  The log scrolls inside the widget's box and follows the newest message.
- **`chat.reset()` drops a turn already in flight.** Its reply comes back
  marked `stale` with no effect, so a slow answer cannot bring the old
  conversation back or submit anything after the person started over.
- **`onEffect` runs after the turn has left the queue**, so a handler that
  calls `chat.report()` queues behind it rather than waiting on itself.

`/client/chat.html` is a working host: a mock policyholder page with an
address on file and its own form, and the chat window beside it. "Fill the
form" prefills the page's form; "Submit" changes the address on file and
reports the reference back.

**`examples/sample_app/`** is the same thing as a separate application, the
way a real host would be built: its own server on its own port, its own
customer record and submit rules, and FillerAI reached only over HTTP. Its
server holds the API token and forwards each turn to `/v1/bot/turn`, adding
the customer's record as `context.current` itself, so the token never reaches
the browser and the page cannot claim someone else's address. A submit goes
through the application's own `/api/address` or `/api/documents`, which can
refuse it (`submit_failed` goes back to the chat with the reason). See its
[README](../examples/sample_app/README.md).

### 7.1 When the browser's speech service is blocked

The browser's recogniser needs the browser vendor's speech service: Chrome and
Edge send the audio to their servers. A VPN, a corporate proxy or a browser
policy can block that while the microphone itself works fine (a Teams call in
the same browser does not use it), and then the microphone hears nothing.

For that case the server can do the transcribing. Start it with
`serve --bot-transcribe` (or `FILLERAI_BOT_TRANSCRIBE=1`) and give it an
**OpenAI** key: `$OPENAI_API_KEY`, one typed into Settings for OpenAI, or
`$FILLERAI_LLM_KEY` when that is an OpenAI key. An Anthropic key is never used
for this, because Anthropic's API does not transcribe audio. Then:

- The Bots tab's microphone **records in the page** instead (`RecordedSpeechInput`,
  with `MediaRecorder`). Recording stops after a short pause once you have
  spoken, after 20 seconds, or when the button is pressed again.
- The recording goes to `POST /api/bot/transcribe` (the UI) or
  `POST /v1/bot/transcribe` (another application), as
  `{"audio": "<base64>", "mime": "audio/webm", "language": "en-US"}`, and comes
  back as `{"text": "...", "via": "speech"}`. The page then sends that text as
  an ordinary spoken turn, so nothing about §3 changes.
- The server passes it to OpenAI's `/v1/audio/transcriptions` with
  `gpt-4o-mini-transcribe` (`$FILLERAI_TRANSCRIBE_MODEL` to change it, for
  example to `whisper-1`; `$FILLERAI_TRANSCRIBE_BASE_URL` for a proxy).

A host application passes `transcribe` to `ChatWidget` to get the same
microphone: `transcribe: (audio) => client.transcribe(audio)`, or a function
that posts to its own backend. `examples/sample_app` does the latter with
`--server-speech`. Like `--bot-llm`, this is off unless asked for, because it
sends people's voices to OpenAI.

---

## 8. Building templates

Three ways, all writing the same thing to the library.

- **The Bots tab** in the FillerAI UI: pick a template or add a starter, or
  start from a schema in the library (or the one on the Schema step), edit the
  examples and the fields table, save. The chat beside the editor runs the
  same turn through `/api/bot/turn`, so a template can be tried before
  anything is connected. The chat tries only the template that is picked, and
  picking another starts a new chat on it. Above it a **sample form** shows the template the
  conversation is on and follows every reply: a value appears the moment it
  is understood, changed fields are green with what they were, and fields the
  chat still needs (missing, or outdated by a change) are amber. "Fill the
  form" hands it over to be submitted by hand; "Submit" writes the values into
  what the application holds on file (the JSON under the chat), as a host
  would to its records. **New chat** starts over and puts the form back.
- **`/v1/templates`** from another program, with an unpinned token.
- **The command line**: `fillerai bot add file.json` or
  `fillerai bot add --starter address_change`, `fillerai bot list`,
  `fillerai bot remove KEY`, and `fillerai bot chat --current city=Austin …`
  for a conversation at the terminal.

A template gets better from real phrasings. The examples are what intent is
matched against, and a field's aliases are what values are found by, so when
the bot misreads something a person said, adding that phrasing to the
template is usually the whole fix.

---

## 9. What this does not do

- **It does not know a city from a street without a label or an address
  shape.** "we're moving in with my sister in Tustin" names a city the local
  reader cannot find; it asks instead. That is the case for `--bot-llm`.
- **It has not been run against a real model.** The language-model reader is
  tested with canned answers only, for the same reason as the rules feature:
  no session has had a key.
- **Speech recognition is the browser's.** It works where the browser has a
  recogniser (Chrome, Edge, Safari), the page is on `https://` or
  `http://localhost` (browsers refuse the microphone on any other `http://`
  address, including a LAN IP or a machine name), and the microphone is
  allowed by both the page and the operating system. Firefox has no
  recogniser, and Brave turns its speech service off. In each of those cases
  the microphone is shown greyed out and pressing it says why; a refusal or
  failure while listening, including hearing nothing, is said in the chat in
  plain words (`SpeechInput.problem()` and `SpeechInput.explain(code)`).
  FillerAI never receives audio unless it was started with
  `--bot-transcribe` (§7.1), but the browser's
  recogniser may send it to its vendor (Chrome sends it to Google), so a host
  that must keep audio in-house should pass `speech: false` to `ChatWidget`
  and bring its own recogniser to `chat.speak(text)`. It was checked in a headless browser, which has no
  microphone, so the spoken path is tested only as far as `via: "speech"`.
- **English only.** The field words, action phrases and question wording are
  English; a template's own examples and aliases can be any language, but the
  bot's replies are not.
- **One conversation is one request.** A phrase that asks for two things at
  once ("change my address and send me an ID card") is read as the first, and
  the second is asked for after.

