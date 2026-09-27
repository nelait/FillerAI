# Northwind Mutual: a sample application using the FillerAI chat

A made-up insurer's customer portal. It shows a signed-in customer's details,
their recent requests, a form to change their address and a form to request a
policy document, and a chat window in the corner. The chat is FillerAI's;
everything else belongs to the application.

It is a separate program on its own port. It does not import FillerAI and
reaches it only over HTTP, the way your own application would.

## Run it

1. Start FillerAI and give it the two starter templates:

   ```sh
   fillerai bot add --starter address_change
   fillerai bot add --starter document_request
   fillerai serve                                  # http://localhost:8000
   fillerai tokens add <your user> --name northwind  # prints flr_...
   ```

   With `fillerai serve --no-auth` no token is needed.

2. Start the sample application:

   ```sh
   python examples/sample_app/app.py --fillerai http://localhost:8000 --token flr_...
   ```

   The token can also come from `$FILLERAI_TOKEN`. Open it as `localhost`:
   browsers only allow the microphone on `https://` or `http://localhost`, so
   the chat can't hear you at a LAN address such as `http://192.168.1.20:8100`.

3. Open http://localhost:8100 and try *"please update my city from SFO to
   Irvine and house no 1429 Silverstein"*. The chat asks for the state and the
   ZIP code, since a new city makes the old ones wrong. Then pick one:
   - **Fill the form for manual submission** puts the values into the
     address form, with what each one was, for you to check and save.
   - **Submit** saves the change through the application's own rules,
     straight away.

   *"Send me my ID card"* goes the same way for a document. **New chat** starts
   over and puts the forms back to what is on file.

The customer's record is kept in `portal-data.json` next to `app.py`; delete
it to start again with the original customer.

## How it is put together

```
browser (static/app.js)             app.py (this application)              FillerAI
───────────────────────             ──────────────────────────             ────────
ChatWidget + BotChat ── POST /api/chat ──► adds context.current from the
                                           customer record, and the token ──► POST /v1/bot/turn
                     ◄──────────────────── the reply, unchanged ◄────────────
onEffect "submit"    ── POST /api/address ─► checks it, saves it, returns
                        /api/documents      a reference
chat.report("submitted", {reference}) ─► /api/chat ─────────────────────────► (closes the chat)
```

Three decisions in there are the ones a real host should copy:

- **The token stays on the server.** The browser talks only to this
  application, which adds `Authorization: Bearer ...` when it forwards a turn.
  `fillerai.js` and its stylesheet are fetched through the application too
  (`/fillerai.js`), so the page needs nothing from FillerAI's origin.
- **The record on file is the server's to say.** `/api/chat` throws away any
  `context.current` the page sends and puts the customer's record there
  itself, so a page cannot ask for a change against somebody else's address.
- **A submit goes through the application's own path.** The chat's "Submit"
  and the form's Save button both post to `/api/address` or `/api/documents`,
  which apply the same rules (a real ZIP code, a two-letter state, a policy
  that is on this account). A refusal goes back to the chat as
  `submit_failed` with the reason, and the chat offers to try again or fill
  the form instead. FillerAI never writes to this application.

The files: `app.py` (the server, standard library only), `static/index.html`,
`static/app.js` (about 30 lines of it are the chat) and `static/style.css`.
`tests/test_sample_app.py` runs it against a real FillerAI.
