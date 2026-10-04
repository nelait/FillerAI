# Security and data handling

What AIrForms protects, from whom, and how; what can leave the machine and
under which switch; and what to change before letting anybody other than
yourself reach it. The reasoning behind each credential is in
[architecture.md](architecture.md) §6 and §7; this page is the operational
view of the same decisions.

Checked against the code at version 0.15.0.

**Contents**

1. [What is being protected](#1-what-is-being-protected)
2. [What can leave the machine](#2-what-can-leave-the-machine)
3. [People: passwords and sessions](#3-people-passwords-and-sessions)
4. [Applications: API tokens](#4-applications-api-tokens)
5. [Language-model keys](#5-language-model-keys)
6. [The two HTTP surfaces](#6-the-two-http-surfaces)
7. [Data at rest](#7-data-at-rest)
8. [Running without accounts](#8-running-without-accounts)
9. [The sample application](#9-the-sample-application)
10. [Before exposing it beyond localhost](#10-before-exposing-it-beyond-localhost)
11. [What is not defended against](#11-what-is-not-defended-against)

---

## 1. What is being protected

| Asset | Where it is | Why it matters |
|---|---|---|
| Real form data, if anyone trains on it | datasets and models in the library | The whole design assumes this data may not leave its environment ([assumptions.md](assumptions.md) §1.1). A model trained on real records carries value counts from them. |
| Each person's library | `entries` / `payloads` in SQLite, one owner per entry | People must not see each other's forms, datasets or models. |
| Passwords | `users.password_hash` | Reused elsewhere, as passwords are. |
| API tokens | `api_tokens.secret_hash`; the secret itself only with the application | A token reads its owner's models and templates and runs bot turns. |
| Language-model API keys | the environment, or server memory | Spend money on somebody's account. |
| What end users type or say into the chat | in flight only | Personal details such as addresses. |

## 2. What can leave the machine

By default, **nothing**. Only `fillerai/llm/transport.py` opens a socket to
the outside, and no module outside `fillerai/llm/` imports that package at
module scope; `tests/test_llm_fence.py` fails if one does. The exceptions are
all explicit:

| Switch | Sends | To | Needs |
|---|---|---|---|
| `fillerai propose-rules`, or **Propose rules** in the Schema step | field names, labels, options and screen names of one form; no records | Anthropic or OpenAI | a key, and the command or button |
| `serve --bot-llm` / `FILLERAI_BOT_LLM=1` | each chat phrase an end user types, with the template's field list | Anthropic or OpenAI | a key **and** the switch; a key alone is not enough |
| `serve --bot-transcribe` / `FILLERAI_BOT_TRANSCRIBE=1` | recordings of end users' voices | OpenAI's transcription endpoint | an OpenAI key **and** the switch |
| The browser's own speech recognition (the chat's microphone without `--bot-transcribe`) | audio of the speaker | whichever service the browser uses (Google, for Chrome) | the person pressing the microphone |

The last row is not AIrForms's traffic but it is the one most easily
forgotten: Chrome's Web Speech API sends audio to Google. It is documented in
[bot-builder.md](bot-builder.md) §7 rather than hidden. A deployment where
voices may not leave should use `--bot-transcribe` against an approved
endpoint (`FILLERAI_TRANSCRIBE_BASE_URL`) or not offer the microphone.

`FILLERAI_LLM_BASE_URL` and `FILLERAI_TRANSCRIBE_BASE_URL` point the
language-model features at a different endpoint, such as a proxy or a
gateway your organisation approves.

## 3. People: passwords and sessions

`fillerai/auth.py`.

- **Hashing.** `hashlib.scrypt` at interactive parameters (roughly 45 ms and
  16 MB per attempt), with PBKDF2 as a fallback for a Python built without
  scrypt. Both formats verify.
- **Rules.** Usernames match `^[a-z0-9][a-z0-9._-]{1,31}$`. Passwords are 8
  to 1,024 characters (the upper bound stops a 10 MB paste being hashed).
- **Guessing.** After 6 failed attempts an account pauses for 15 minutes and
  then clears itself.
- **Sessions are server-side.** The cookie carries a random id and a
  signature; the session row is in the database. A session expires after 12
  idle hours and lives 7 days at most. Signing out deletes the row, so it
  takes effect everywhere at once, and changing a password ends every
  session of that user.
- **The cookie** is `HttpOnly` and `SameSite=Strict`. It is **not** marked
  `Secure`, because the server speaks plain HTTP on loopback and a browser
  would drop a `Secure` cookie there. Behind an HTTPS proxy, see §10.
- **CSRF.** Every `/api` POST must echo the session's token in the
  `X-FillerAI-Token` header, compared in constant time. Another origin cannot
  read that token, so it cannot forge the header.
- **Response headers.** Every response carries `X-Content-Type-Options:
  nosniff` and `X-Frame-Options: DENY`.
- **The first administrator.** When the database has no users, `serve`
  creates `admin` with a generated password printed once to the console
  (or the one in `FILLERAI_ADMIN_PASSWORD`), which must be changed at first
  sign-in.
- **The last administrator** cannot be disabled, demoted or deleted.

## 4. Applications: API tokens

`fillerai/tokens.py`.

- The token is `flr_<12 hex id>_<secret>`, with 24 random bytes (32 URL-safe characters) from
  `secrets`. It is shown **once**, when issued.
- Only a SHA-256 hash of the secret is stored. That is deliberate: the
  secret is unguessable, so slow hashing buys nothing, and it is checked on
  every call.
- A token acts as the user who issued it and can reach only that user's
  library. It can be limited to one model and given an expiry of 1 to 3,650
  days.
- Disabling or deleting the user revokes every token they issued.
- Keep the token on the application's **server**, never in the browser. The
  sample application shows the pattern: its page talks to its own server,
  which adds the token and forwards to AIrForms ([integration.md](integration.md)).

## 5. Language-model keys

`fillerai/llm/config.py`, `fillerai/web/keyring.py`.

- The durable place for a key is the environment: `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, or the neutral `FILLERAI_LLM_KEY`.
- A key typed into **Settings** is held in the server process, per user, and
  written **nowhere**: not the database, the library, a config file or a log.
  Restarting the server forgets it, and the UI says so.
- No command prints a key; `llm status` shows the first four characters.
- A test serialises every reply the UI gets and scans the library directory
  to check the key appears in neither.

## 6. The two HTTP surfaces

| | `/api` | `/v1` |
|---|---|---|
| For | the AIrForms UI | other applications |
| Credential | session cookie + CSRF header | `Authorization: Bearer` only |
| Reads the other's credential | never accepts a bearer token | never reads the cookie (`Handler._rest` installs an empty context first) |
| Cross-origin | same origin only | `*` by default with accounts, since a token is needed anyway and `Allow-Credentials` is never sent; with `--no-auth`, no origin until `--cors-origin` names one |
| Body limit | 8 MB | 8 MB |

The reason `/v1` refuses the cookie: if it honoured it, any web page the user
has open could drive it with their session.

### The documentation site

`/docs` is a third, read-only surface: the Markdown in `docs/` and the user
guide, rendered on the server with every byte escaped and only `http`,
`https` and `mailto` links kept. It is opened by an access code rather than
an account, so it can be shared with people who have none. The code is kept
as a scrypt hash; the browser gets an HttpOnly cookie scoped to `/docs` whose
value is an HMAC keyed by that hash, so it cannot be made without the code and
dies when the code changes. Wrong codes are limited to 8 per address per 15
minutes. With no code set the docs are closed to everybody, including
administrators.

## 7. Data at rest

- **The database** is one SQLite file, `fillerai.db` in the library
  directory, unless `--database` says otherwise. It is not encrypted; protect
  it with file permissions and disk encryption like any other data file. It
  holds password hashes, token hashes, session ids and every library entry.
- **The file library** (the CLI's default, `./.fillerai` or
  `$FILLERAI_HOME`) is plain JSON you can read with `cat`.
- **Library entries are owned, and their contents are never rewritten**
  (only the display name can change); deleting one deletes it for
  good.
- **Generated identifiers** come from ranges that cannot belong to a real
  person (SSN areas 900-999, the 555-01xx fiction block, RFC 2606 email domains,
  card test ranges; see the README's "Identifiers cannot belong to a real
  person"), unless `--realistic-identifiers` was passed, so a
  synthetic dataset is safe to share.
- **Trained models** hold value counts and, for the nearest-records engine,
  a bounded sample of the training records themselves. A model trained on real data should be
  treated as that data.
- **The sample application** keeps its demo customer in
  `<library>/sample-app/`.

## 8. Running without accounts

`serve --no-auth` turns off sign-in: anyone who can reach the port is the one
user. It is therefore **refused unless the server binds to localhost**, and
with it the `/v1` API allows no browser origin until `--cors-origin` names
one. It is meant for one person on their own machine.

## 9. The sample application

`fillerai serve` also starts the Northwind Mutual demo on port 8100, bound to
the same host as AIrForms, and issues it an API token called "Sample
application" for the first administrator (or `--sample-user`). The demo has
**no sign-in of its own**: anyone who reaches its port can chat with that
user's templates and submit to the demo customer's record, from the chat or
by opening any form from its menu. On a shared host
start the server with `--no-sample-app`, or keep it on loopback.

The same demo is also passed through at `/sample/` on AIrForms' own port.
There it **does** need an AIrForms sign-in (a GET is sent to `/login`, any
other request gets a 401), unless `--sample-public` /
`FILLERAI_SAMPLE_PUBLIC=1` opens it on purpose. Its POSTs need no CSRF token:
the session cookie is `SameSite=Strict`, so another site's request arrives
without it and is refused. On a host that exposes only one port (Railway),
`/sample/` is the only way in.

## 10. Before exposing it beyond localhost

AIrForms is built as a local working tool ([assumptions.md](assumptions.md)
§1.3). If more than one machine needs to reach it:

1. **Keep accounts on.** `--no-auth` is refused off loopback anyway.
2. **Put it behind an HTTPS reverse proxy** and keep AIrForms itself on
   `127.0.0.1`. The server speaks plain HTTP only. Have the proxy add
   `Secure` to the `fillerai_session` cookie if it can, and HSTS.
3. **Start with `--no-sample-app`**, or accept that its port is open to
   whoever can reach it (§9).
4. **Narrow CORS** with `--cors-origin https://your-app.example` rather than
   the default `*` if only known applications should call `/v1` from a
   browser.
5. **Change the first administrator's password** (you are forced to) and
   create named accounts rather than sharing it.
6. **Issue one token per application**, limited to one model where it only
   needs one, with an expiry.
7. **Decide about the microphone** (§2) before end users see it.
8. **Back up the database file** ([operations.md](operations.md) §5); it is
   the only copy of the accounts and the library.

## 11. What is not defended against

- **Somebody with the machine.** Anyone who can read the database file or
  the process's memory has everything in it, including keys typed into
  Settings for the life of the process.
- **An administrator.** Administrators manage accounts; they are trusted.
- **Denial of service.** There are body-size limits and a sign-in pause, not
  rate limiting on `/v1` or the training endpoints. A proxy is the place for
  that.
- **What a third-party model provider does with data** sent to it under the
  switches in §2. That is a matter for the agreement with the provider.

See also [pending.md](pending.md) for known defects.
