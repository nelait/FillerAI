# Northwind Mutual: a sample application using the FillerAI chat

A made-up insurer's customer portal. It shows a signed-in customer's details
and their recent requests, a form for **every template the FillerAI bot
service has**, and a chat window in the corner. The chat is FillerAI's;
everything else belongs to the application.

It is a separate program on its own port. It imports nothing from the rest of
FillerAI and reaches it only over HTTP on `/v1`, the way your own application
would. It lives inside the package only so that `fillerai serve` can start it.

## Run it

`fillerai serve` starts it next to the UI:

```sh
fillerai serve          # UI on http://localhost:8000, sample app on http://localhost:8100
```

The UI's header has a **Sample app** link to it. With accounts on, `serve`
issues it an API token called "Sample application" for the first
administrator (or `--sample-user <name>`), replacing the last one, so its
forms are that account's bot templates. `--sample-port` moves it and
`--no-sample-app` leaves it off.

It can also be run on its own against any FillerAI:

```sh
python -m fillerai.sampleapp --fillerai http://localhost:8000 --token flr_...
```

The token can also come from `$FILLERAI_TOKEN`; a FillerAI started with
`--no-auth` needs none. Open it as `localhost`: browsers only allow the
microphone on `https://` or `http://localhost`.

## Try it

Add the two starters on FillerAI's Bots tab (or `fillerai bot add --starter
address_change`), reload the sample application, and there is a form for
each. Then, in the chat:

- *"please update my city from SFO to Irvine and house no 1429 Silverstein"*
  fills in the address form as you talk, green where a value changed, with
  what it was, and amber where something is still needed. A new city makes
  the old state and ZIP code wrong, so the chat asks for them.
- *"document request"* in the middle of that moves to the document form.
- **Submit** in the chat submits the form through the application's own
  path; **Fill the form for manual submission** leaves it filled for you to
  check and submit.

Every form can also be filled and submitted by hand, like any other
application's. **New chat** starts over and puts the forms back to what is on
file. A template saved on the Bots tab is a form here after a reload.

The customer's record is kept in the library directory, under
`sample-app/` (or `portal-data.json` when run on its own); delete it to start
again with the original customer.

## How it is put together

```
browser (static/app.js)             app.py (this application)              FillerAI
───────────────────────             ──────────────────────────             ────────
page load            ── GET /api/templates ─► with the token ─────────────► GET /v1/templates
                                                                            GET /v1/templates/<key>
                     ◄── one form per template ◄───────────────────────────
ChatWidget + BotChat ── POST /api/chat ──► adds context.current from the
                                           customer record, and the token ──► POST /v1/bot/turn
                     ◄──────────────────── the reply, unchanged ◄────────────
onReply              fills the form the reply is about
onEffect "submit"    ── POST /api/submit/<template> ─► checks it, saves it,
                                                       returns a reference
chat.report("submitted", {reference}) ─► /api/chat ─────────────────────────► (closes the chat)
```

Four decisions in there are the ones a real host should copy:

- **The token stays on the server.** The browser talks only to this
  application, which adds `Authorization: Bearer ...` when it forwards a turn.
  `fillerai.js` and its stylesheet are fetched through the application too
  (`/fillerai.js`), so the page needs nothing from FillerAI's origin.
- **The record on file is the server's to say.** `/api/chat` throws away any
  `context.current` the page sends and puts the customer's record there
  itself, so a page cannot ask for a change against somebody else's address.
- **The template decides the form, the application decides what it accepts.**
  `/api/submit/<template>` reads the template from FillerAI again rather than
  trusting the page's copy, checks required fields and options against it,
  and adds its own rules by semantic type: a real ZIP code, a two-letter
  state, an email address, a policy that is on this account. A value changes
  the customer's record only where the record has that field; the rest (which
  document, how to send it) is kept with the request.
- **A submit goes through the application's own path.** The chat's "Submit"
  and the form's Submit button both post to `/api/submit/<template>`. A
  refusal goes back to the chat as `submit_failed` with the reason, and the
  chat offers to try again or fill the form instead. FillerAI never writes to
  this application.

The files: `app.py` (the server, standard library only), `static/index.html`,
`static/app.js` and `static/style.css`. `tests/test_sample_app.py` runs it
against a real FillerAI.
