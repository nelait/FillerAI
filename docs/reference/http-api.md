# HTTP API reference

Every route FillerAI serves over HTTP, in one place. `fillerai serve` answers
on two surfaces from one port: `/api`, which is the UI talking to its own
server, and `/v1`, the integration service for other applications. It also
serves the UI's pages and the JavaScript client. The sample application
(Northwind Mutual) is a separate server on its own port and is listed at the
end.

`/v1` already has a narrative guide in [integration.md](../integration.md)
(models) and [bot-builder.md](../bot-builder.md) (templates and the chat), so
its entries here are a compact table that links there. `/api` has no other
reference, so the detail is here.

This reflects version 0.15.0. The code is `fillerai/web/server.py` (routing,
`/api`, static files), `fillerai/web/rest.py` and `fillerai/web/botrest.py`
(`/v1`), and `fillerai/sampleapp/app.py` (the sample application).

## Contents

1. [Rules that apply everywhere](#1-rules-that-apply-everywhere)
2. [Session and accounts](#2-session-and-accounts)
3. [Admin](#3-admin)
4. [Library](#4-library)
5. [Schema and extract](#5-schema-and-extract)
6. [Generate](#6-generate)
7. [Train](#7-train)
8. [Predict](#8-predict)
9. [Simulate](#9-simulate)
10. [LLM settings and rules](#10-llm-settings-and-rules)
11. [Bots and templates](#11-bots-and-templates)
12. [API tokens](#12-api-tokens)
13. [Pages, static files and the client](#13-pages-static-files-and-the-client)
14. [`/v1` models](#14-v1-models)
15. [`/v1` bot](#15-v1-bot)
16. [Sample application](#16-sample-application)

---

## 1. Rules that apply everywhere

### Which surface a request lands on

The path decides, before anything else:

| Path | Surface | Methods |
|---|---|---|
| `/v1`, `/v1/...` | integration API | `GET`, `POST`, `OPTIONS` (preflight), `HEAD` |
| `/api/...` | the UI's API | `POST` for every route; `GET` also works for `/api/meta` only |
| `/`, `/index.html`, `/login` | the UI's pages | `GET`, `HEAD` |
| `/static/...`, `/client/...` | files | `GET`, `HEAD` |

Anything else is a plain-text `404 Not found`. A `GET` to any `/api` route
other than `/api/meta` is also that plain-text 404, not a JSON error.
`OPTIONS` on anything outside `/v1` is a 404 with an empty body.

### `/api`: session cookie and CSRF header

With accounts on (the default), `/api` works like this:

- **Sign in** with `POST /api/auth/login`. The reply sets the cookie
  `fillerai_session` (`Path=/; HttpOnly; SameSite=Strict`, no `Secure`, since
  it is served over plain http on loopback) and carries a `csrf` string.
- **Every later `/api` call** must send that string back in the header
  `X-FillerAI-Token`. Only `/api/auth/login` and `/api/meta` are exempt. A
  missing or wrong header is `403 {"error": "this page is out of date - reload
  it", "stale": true}`. A browser that has lost the string can get it again
  from `GET /api/meta`, which is exempt and includes `csrf` when signed in.
- **Signing in again, and changing your own password,** both issue a new
  session with a new `csrf`. Signing out clears the cookie
  (`Max-Age=0`).

Each route has one of three access levels, set in the `ROUTES` table in
`server.py` and checked in one place (`Handler._guard`) before the handler
runs:

| Access | Who | Refusal when not met |
|---|---|---|
| anonymous | anyone | — |
| user | any signed-in, active account | `401 {"error": "sign in to use FillerAI", "sign_in": true}` |
| admin | an account with role `admin` | `403 {"error": "that is an administrator's to do"}` |

Only `/api/meta` and `/api/auth/login` are anonymous. Every other `/api`
route needs at least a signed-in user.

**An account that must change its password** (a new account, or one an
administrator reset) may call only `/api/auth/me`, `/api/auth/password`,
`/api/auth/logout` and `/api/meta`. Anything else is
`403 {"error": "choose a new password before carrying on", "must_change":
true}`. This check comes before the CSRF check.

The guard runs in this order: signed in, admin, must-change, CSRF. The
request body is read and thrown away (up to 16 MB) before a refusal is sent,
so a browser partway through an upload receives the reason instead of a
broken pipe.

Which library a signed-in user reads and writes is their own: one library
per account inside the database.

### `/v1`: bearer tokens, never the cookie

`/v1` takes `Authorization: Bearer flr_...` and nothing else. The server
installs an empty request context before handling a `/v1` call, so the
session cookie is not read even if the browser sends it. A token belongs to
one account and reads that account's library; a token can be pinned to one
model (see [integration.md](../integration.md#authenticating)).

| Status | `code` | Cause |
|---|---|---|
| 401 | `no_token` | no `Authorization: Bearer` header |
| 401 | `bad_token` | not a token, revoked, expired, or its account has been deleted |
| 403 | `account_off` | the token's account is disabled |

`GET /v1/health` is the only `/v1` route that needs no token. A successful
call stamps the token's `last_used`.

### `--no-auth`

`fillerai serve --no-auth` is the single-user tool. It is refused unless the
host is loopback (`127.0.0.1`, `::1`, `localhost`); the command exits with
status 1 and says why.

In this mode:

- There are no accounts. The guard lets every `/api` request through with no
  cookie and no CSRF header.
- Without `--database`, the library is the file library on disk
  (`--library`, or `$FILLERAI_HOME`, or `./.fillerai`). With `--database`,
  it is one unowned library in that database.
- `/api/auth/login`, the `/api/admin/users...` routes and `/api/auth/password`
  answer `404 {"error": "this server is running without accounts"}`.
  `/api/auth/me` answers `401 {"error": "sign in first"}`, since nobody is
  signed in. `/api/auth/logout` answers `{"signed_out": true}` and does
  nothing.
- `/api/admin/database` and `/api/admin/import` answer 404 unless a
  `--database` was given.
- The `/api/tokens...` routes answer `409`: a token names an account, and there
  are none.
- `/v1` answers without a token. An `Authorization` header, if sent, is
  ignored.
- `GET /login` redirects to `/`.

### CORS

Only `/v1` takes part in CORS. `/api` never sends CORS headers and never
answers a preflight, because the UI is same-origin by construction.

Which origins may call `/v1` from a browser comes from `allowed_origins()`:

| Server started with | Allowed origins |
|---|---|
| `--cors-origin ORIGIN` (repeatable) | exactly those; `*` if given |
| accounts on, no `--cors-origin` | `*` |
| `--no-auth`, no `--cors-origin` | none |

The reasoning: with accounts on, a caller needs a bearer token it had to be
given, so any origin is safe; with `--no-auth` there is no credential, and
`*` would let any open page read the library.

What the server sends:

- Allowed is `*`: `Access-Control-Allow-Origin: *`.
- The request's `Origin` is in the list: `Access-Control-Allow-Origin:
  <origin>` and `Vary: Origin`.
- The list is not empty but the origin is not in it: `Vary: Origin` only.
- The list is empty: no CORS headers.

`Access-Control-Allow-Credentials` is never sent.

A preflight (`OPTIONS /v1/...`) answers `204` with
`Access-Control-Allow-Methods: GET, POST, OPTIONS`,
`Access-Control-Allow-Headers: Authorization, Content-Type` and
`Access-Control-Max-Age: 600` when the origin is allowed, and `403` with an
empty body when it is not.

### Request bodies

Both surfaces take a JSON object as the body of a `POST`. The body is
optional; an empty one is `{}`.

| Problem | `/api` | `/v1` |
|---|---|---|
| `Content-Length` not a number | 400 `bad Content-Length` | 400, `code: bad_request` |
| over 8 MB | 413 `that source is too large for the UI`; connection closed | 413, `code: too_large`; connection closed |
| not valid JSON, or not UTF-8 | 400 `request body is not valid JSON: ...` | 400, `code: bad_json` |
| valid JSON but not an object | 400 `request body must be a JSON object` | 400, `code: bad_json` |

### Error bodies

**`/api`** errors are `{"error": "a sentence for a person"}` with the status
set. A few guard refusals add one flag the UI acts on: `sign_in`, `stale` or
`must_change`, each `true`. An unknown `/api` path on `POST` is `404 {"error":
"no endpoint at /api/..."}`. An unexpected exception is `500 {"error":
"unexpected failure: ..."}`, and the traceback goes to the server's console.

**`/v1`** errors are `{"error": "a sentence", "code": "a_short_code"}`. Branch
on `code`, show `error`. Beyond the codes in each section:

| Status | `code` | Cause |
|---|---|---|
| 404 | `not_found` | no route at that path (including bare `/v1`) |
| 405 | `method_not_allowed` | the path exists under the other method |
| 400 | `bad_request` | an `/api`-style error raised underneath a `/v1` call |
| 500 | `server_error` | an unexpected exception |

### Response headers

Every response from the main server carries `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`. JSON is
`application/json; charset=utf-8`.

### Server-side limits worth knowing

| Limit | Value | Where it shows |
|---|---|---|
| request body | 8 MB | both surfaces |
| records per generate or train call | 5000 | `/api/generate`, `/api/train*` |
| cached UI models | 4, least recently used first out | `model_id` stops working: 404 |
| kept training runs | 6 | `run_id` stops working: 404 |
| log lines per poll | 400 | `/api/train/log` |
| sweep cases | 200 | `/api/simulate/sweep` |
| API key length | 512 characters | `/api/llm/key` |
| tokens per account | 25 | `/api/tokens/create` |

---

## 2. Session and accounts

### `POST /api/meta` (also `GET`)

**Access:** anonymous.

What the front end needs to draw itself. Signed out, with accounts on, it
answers only:

```json
{"version": "0.15.0", "accounts": true, "signed_in": false}
```

Signed in, or with `--no-auth`, it adds:

| Field | Meaning |
|---|---|
| `user` | the signed-in user (see below); absent with `--no-auth` |
| `csrf` | the session's CSRF string; absent with `--no-auth` |
| `semantic_types` | every semantic type a field may have |
| `examples` | bundled examples: `{id, kind: "html" \| "spec", label, name}` |
| `algorithms`, `default_algorithm` | training algorithms and their tuning knobs |
| `library` | where the library is: a directory, or a database URL |
| `bot_llm`, `bot_transcribe` | whether `--bot-llm` / `--bot-transcribe` are on |
| `sample_app_port` | the sample application's port, or `null` |

Errors: none of its own.

A **user** object, wherever one appears, is `{id, username, display_name,
role, active, created, last_login, must_change}`. It never carries the
password hash.

### `POST /api/auth/login`

**Access:** anonymous. Not CSRF-checked.

| Body | |
|---|---|
| `username` | required in practice |
| `password` | required in practice |
| `agent` | optional; recorded on the session |

Returns `{user, csrf, must_change}` and sets the session cookie.

| Status | Cause |
|---|---|
| 401 | `that username and password do not match` (same message for an unknown name) |
| 403 | `that account has been turned off` |
| 429 | too many failed attempts: 6 failures lock the account for 15 minutes |
| 404 | `--no-auth`: `this server is running without accounts` |

### `POST /api/auth/logout`

**Access:** user.

Ends the session on the server and clears the cookie. Returns
`{"signed_out": true}`.

### `POST /api/auth/me`

**Access:** user. Allowed while a password change is due.

Returns `{user, csrf, admin, must_change}`. `admin` is a boolean.

Errors: 401 `sign in first` when nobody is signed in, which is always the
case with `--no-auth`.

### `POST /api/auth/password`

**Access:** user. Allowed while a password change is due.

| Body | |
|---|---|
| `current` | the password now |
| `new` | the new password: at least 8 characters, not the username |

Ends every session the account had, then starts a new one for this browser.
Returns `{user, csrf, changed: true}` and sets a new cookie; use the new
`csrf` from here on.

| Status | Cause |
|---|---|
| 403 | `that is not your current password` |
| 400 | the new password is the same as the old one, too short, too long, or the username |
| 404 | `--no-auth` |

---

## 3. Admin

Every route here is **admin** only, and every one needs accounts on. With
`--no-auth` they answer 404.

### `POST /api/admin/users`

Every account, with what each has in the library.

Returns `{users, roles, admins, you}`: `users` are user objects with two
more fields, `entries` (library entries the account owns) and `sessions`
(open sessions). `roles` is `["admin", "user"]`, `admins` is how many active
administrators there are, and `you` is the caller's id.

### `POST /api/admin/users/create`

| Body | |
|---|---|
| `username` | 2-32 characters: lowercase letters, digits, `.`, `-`, `_`; starts with a letter or digit |
| `password` | optional; generated if empty |
| `role` | `user` (default) or `admin` |
| `display_name` | optional |
| `must_change` | optional; forced on when the password was generated |

Returns `{user, password}`. `password` is the generated one, shown this once,
or `null` if the caller gave one.

| Status | Cause |
|---|---|
| 400 | bad username, bad role, or a password that fails the rules |
| 409 | `there is already a user called '...'` |

### `POST /api/admin/users/update`

| Body | |
|---|---|
| `id` | required: the user's id |
| `display_name` | optional |
| `role` | optional: `admin` or `user` |
| `active` | optional: `false` turns the account off, which also revokes its tokens |

Only the fields present are changed. Returns `{user}`.

| Status | Cause |
|---|---|
| 400 | `missing 'id' in request`; bad role; demoting or disabling the only active administrator |
| 404 | `there is no such user` |

### `POST /api/admin/users/password`

Reset somebody's password to one they must change at next sign-in.

| Body | |
|---|---|
| `id` | required |
| `password` | optional; generated if empty |

Returns `{user, password}` (generated password or `null`). Errors: 400 for a
password that fails the rules or a missing `id`, 404 for an unknown user.

### `POST /api/admin/users/delete`

Removes the account, its tokens, and everything in its library.

| Body | |
|---|---|
| `id` | required |

Returns `{removed: <id>, username}`.

| Status | Cause |
|---|---|
| 400 | `you cannot delete the account you are signed in as`; the only administrator left |
| 404 | unknown user |

### `POST /api/admin/database`

Where the data is. No body.

Returns `{database: {url, backend, version, tables}, entries, file_library,
file_library_exists}`. `entries` counts every account's entries together;
`file_library` is the directory a file library would be in.

Errors: 404 `this server is running without a database`.

### `POST /api/admin/import`

Copies the file library on disk into the caller's own library, keeping ids
and lineage. Entries already there are skipped, so it is safe to run twice.
No body.

Returns `{copied, skipped, failed, from}`: counts for the first two, the
list of failures, and the directory copied from.

Errors: 404 without a database.

---

## 4. Library

**Access:** user, for every route here. Each reads and writes the caller's
own library.

Library entries have kinds `source`, `schema`, `dataset`, `model`, `script`
and `template`. An **entry** object is `{id, kind, name, created, parent, meta,
bytes}`. Ids look like
`mdl-20260927-143242773-e101`.

### `POST /api/library`

The library, with lineage drawn out so the panel can walk it both ways.

| Body | |
|---|---|
| `kind` | optional: one of the kinds above |
| `under` | optional: only entries whose parent is this id |
| `limit` | optional: 1-2000, default 200 |

Returns `{root, totals, bytes, entries}`. `totals` counts each kind; each
entry has two more fields, `lineage` (ancestor ids, oldest first) and
`children` (child ids).

Errors: 400 for an unknown `kind` or a `limit` out of range.

### `POST /api/library/open`

One entry, ready for the stage it belongs to.

| Body | |
|---|---|
| `id` | required |

Always returns `{entry, lineage, children}` as full entry objects. It also
fills in whatever is above the entry: `schema` and `schema_id`, `source`,
`source_kind` and `source_id`, `dataset_id`. Then, by the entry's kind:

| Kind | Adds |
|---|---|
| `script` | `script`: the Python source |
| `dataset` | `records` |
| `model` | the model is loaded into the UI's cache: `model_id` (a handle for `/api/predict` and `/api/simulate/*`), `algorithm`, `seeds`, `fields`, `drawable`, `engine`, `combiner`, `counts`, `trained_on`, `held_out`, `rules` |

Errors: 404 when there is no such entry or its payload cannot be read.

### `POST /api/library/export`

One entry as the file it would be on disk.

| Body | |
|---|---|
| `id` | required |

Returns `{filename, text}`. A script is `<id>.py` with the Python as text;
anything else is `<id>.<source|schema|records|model>.json`.

Errors: 404 for an unknown id.

### `POST /api/library/delete`

| Body | |
|---|---|
| `id` | required |
| `cascade` | optional: also delete everything descended from it |

Returns `{removed: [ids]}`. Removed models are dropped from the `/v1` cache
too.

Errors: 404 for an unknown id; 400 when the entry has children and `cascade`
is not set.

### `POST /api/library/rename`

| Body | |
|---|---|
| `id` | required |
| `name` | required, non-empty; cut to 120 characters |

Returns `{entry}`. Errors: 404 unknown id, 400 empty name.

---

## 5. Schema and extract

**Access:** user.

### `POST /api/example`

One of the bundled examples in `examples/`, by name. Never an arbitrary
path.

| Body | |
|---|---|
| `id` | required: an `id` from `/api/meta`'s `examples` |

Returns `{id, content}`. Errors: 404 `no bundled example called '...'`.

### `POST /api/extract`

Pasted HTML or a field spec, read into an inferred schema.

| Body | |
|---|---|
| `content` | required, non-empty: the HTML, or the JSON as a string |
| `kind` | `html` (default) or `spec`. A `spec` that is a saved schema (has `schema_version`) is loaded as-is |
| `name` | optional form name; default `form` |
| `save` | default `true`: keep the source and the schema in the library |

Returns `{schema, summary}`, plus `source_id` and `schema_id` when saved.
`summary` is `{fields, screens, groups, needs_review}`; `needs_review` lists
fields inferred with confidence under 0.7 as `{name, semantic_type,
confidence}`.

| Status | Cause |
|---|---|
| 400 | missing or empty `content`; unknown `kind`; invalid JSON; a spec that does not load; no fields found |

### `POST /api/reinfer`

Runs inference again over an edited schema. Fields with confidence 1.0 (set
by hand in the editor) are left alone.

| Body | |
|---|---|
| `schema` | required |

Returns `{schema, summary}`. Nothing is saved. Errors: 400 for a missing or
unreadable schema.

---

## 6. Generate

**Access:** user.

### `POST /api/generate`

Synthetic records for a schema, checked unless asked not to be.

| Body | |
|---|---|
| `schema` | required |
| `count` | 1-5000, default 20 |
| `seed` | whole number, or empty for random |
| `blank_rate` | 0-1, default 0.12 |
| `safe_identifiers` | default `true` |
| `check` | default `true`: validate and run the coherence report |
| `save` | default `true`: keep the records as a dataset |
| `schema_id` | optional: the library schema these belong to. If missing or unknown, the schema is saved now |

Returns `{columns, records, problems, count}`, plus `schema_id` and
`dataset_id` when saved. `columns` leaves out read-only fields; `problems` is
a list of sentences.

Errors: 400 for a bad schema, `count`, `seed` or `blank_rate`.

### `POST /api/export`

Records already in hand, rendered as a file.

| Body | |
|---|---|
| `schema` | required |
| `records` | required: a list |
| `format` | `json` (default), `ndjson` or `csv` |

Returns `{format, text, filename}`. Errors: 400 for a bad schema, `records`
that is not a list, or an unknown format.

---

## 7. Train

**Access:** user.

The training routes share these body fields:

| Body | |
|---|---|
| `schema` | required (optional for `/api/train/script`) |
| `records` | required: a non-empty list of objects, at most 5000 |
| `algorithm` | one of `/api/meta`'s `algorithms`; default is `default_algorithm` |
| `seed` | whole number, or empty for random |
| `tuning` | object of the algorithm's knobs; each must be in the knob's declared range, and undeclared keys are ignored |
| `use_rules` | default `true` |
| `seeds` | optional: fields to treat as asked-first; default is the model's own suggestion |
| `ask` | 1-8, default 3: how many seed fields to suggest |
| `save` | default `true`: keep the dataset, the model and its script |
| `dataset_id`, `schema_id` | optional: where the records already are in the library |

A quarter of the records are held back for evaluation.

Common errors, all 400: missing or unreadable schema; `records` missing,
empty, too many, or containing a non-object; unknown algorithm; a tuning
value that is not a number or out of range; a bad seed.

### The training result

What `/api/train` returns, and what `/api/train/log` hands back as `result`
once a run finishes:

| Field | Meaning |
|---|---|
| `model_id` | a handle to the model in the UI's cache (not a library id) |
| `algorithm` | the algorithm used |
| `seeds` | the fields to ask for first |
| `fields` | per-field report: how each is filled, from what, how strongly |
| `trained_on`, `held_out` | record counts |
| `threshold` | the acceptance threshold, 0.7 |
| `engine` | the engine's summary |
| `combiner` | the combiner's weights, structured |
| `settings` | the options the model was trained with |
| `rules` | derived rules: `{target, inputs, kind, accuracy}` |
| `counts` | `{predictable, yours, total}` |
| `drawable` | fields that have a tree to draw |
| `evaluation` | the score on the held-out records, when there were any |
| `model_entry_id`, `script_entry_id`, `dataset_id` | library ids, when saved |

### `POST /api/train`

Trains in the request and returns the training result. The UI uses
`/api/train/start` instead; this one is kept for the CLI and the tests.

### `POST /api/train/start`

Starts a training run on a worker thread and returns straight away. The run
is saved into the caller's library, not an unowned one.

Returns `{run_id, algorithm, recipe, script, command}`: the algorithm's
description, the steps it will take, the Python script the run is equivalent
to, and the CLI command.

### `POST /api/train/log`

Polls a run.

| Body | |
|---|---|
| `run_id` | required |
| `cursor` | the `cursor` from the last poll; default 0 |

Returns `{run_id, lines, cursor, progress, result, error}`:

- `lines`: up to 400 new lines, each `{index, at, level, text}`.
- `cursor`: pass it back on the next poll.
- `progress`: `{step, steps, fraction, elapsed, finished, failure}`.
- `result`: `null` until the run finishes, then the training result.
- `error`: `null`, or the sentence a failed run ended with. A failed run
  still answers 200; the failure is in the body and the log.

Errors: 404 `that training run is no longer here - start it again` (only the
6 most recent runs are kept).

### `POST /api/train/script`

The script and recipe for a set of options, without training. Takes the
training fields; `schema` and `records` are optional.

Returns `{algorithm, recipe, script, command}`. Errors: 400 for an unknown
algorithm or bad tuning.

### `POST /api/train/tree`

The tree grown for one field, drawn as text lines.

| Body | |
|---|---|
| `model_id` | required |
| `field` | required |

Returns `{field, lines, algorithm}`. `lines` is empty for engines without
trees. Errors: 404 when the model is no longer cached.

### `POST /api/model`

The cached model as the JSON file the CLI reads.

| Body | |
|---|---|
| `model_id` | required |

Returns `{filename, text}`. Errors: 404 when the model is no longer cached.

---

## 8. Predict

**Access:** user.

### `POST /api/predict`

Completes a record from the fields typed so far.

| Body | |
|---|---|
| `model_id` | required: a handle from training or `/api/library/open` |
| `observed` | object of field name to value; default `{}` |
| `threshold` | 0-1, default 0.7 |

Returns `{given, predictions, offered, threshold}`. `predictions` has one
entry per field, with its value, confidence, basis and reasons; `offered`
counts those that are known and at or above the threshold.

| Status | Cause |
|---|---|
| 404 | `that model is no longer loaded - train it again to carry on` |
| 400 | `observed` is not an object; a field the model does not have; bad threshold |

The UI keeps at most four models loaded. A 404 here means train again, or
open the model from the library.

---

## 9. Simulate

**Access:** user. Every route takes `model_id` (required) and answers 404 when
that model is no longer cached.

### `POST /api/simulate/form`

The form to draw and what to ask first.

| Body | |
|---|---|
| `model_id` | required |
| `ask` | 1-8, default 3 |

Returns `{layout, seeds, threshold, effort, assumptions, model}`. `model` is
where the model came from: its own description plus `model_id`,
`algorithm_label`, `entry` (the library entry, or `null` for a model that was
never saved) and `lineage` (entries oldest first, or empty).

### `POST /api/simulate/case`

A fresh record the model has not seen.

| Body | |
|---|---|
| `model_id` | required |
| `seed` | optional; random if empty |

Returns `{case, seed}`. Pass the same `seed` to get the same case again.

### `POST /api/simulate/fill`

Finishes the form from what has been typed and costs the result.

| Body | |
|---|---|
| `model_id` | required |
| `typed` | object of field name to value; default `{}` |
| `case` | optional: the true record, to score against |
| `threshold` | 0-1, default 0.7 |

Returns `{threshold, cells, savings, score, record, headline}`.

Errors: 400 when `typed` or `case` is not an object, a field is unknown, or
the threshold is bad.

### `POST /api/simulate/sweep`

The same simulation over many fresh cases.

| Body | |
|---|---|
| `model_id` | required |
| `count` | 1-200, default 50 |
| `seed` | optional |
| `seeds` | optional: fields typed by hand; default is the model's suggestion |
| `threshold` | 0-1, default 0.7 |

Returns totals over the sweep: `{cases, seeds, threshold, filled, left,
checked, right, wrong, accuracy, share_saved, seconds_by_hand, seconds_now,
seconds_saved, keystrokes_saved, per_case, headline, seed, assumptions}`.

Errors: 400 for a bad `count`, `seed` or threshold.

---

## 10. LLM settings and rules

**Access:** user.

API keys typed here are kept in memory, per account, until the server stops.
No reply ever contains a key, only a redacted form of it. The provider names
are `anthropic` and `openai`.

### The status object

`/api/llm/status`, `/api/llm/key`, `/api/llm/preference` and
`/api/llm/forget` all return this:

| Field | Meaning |
|---|---|
| `providers` | each `{name, label, key_variable, models, source, typed}`; `source` is `typed here`, `environment` or empty |
| `provider`, `label` | which service the next call would use |
| `reason` | why that service was chosen |
| `models` | the model for each task: `{rules, typing, chat}` |
| `bot_llm` | whether the bot reads phrases with a model |
| `key` | the key in redacted form |
| `configured` | whether there is a key to call with |
| `transport` | the SDK or HTTP transport that would be used |
| `endpoint`, `custom_base_url` | where calls go |
| `preference` | the caller's chosen provider and model |
| `key_variable` | the generic environment variable for a key |

### `POST /api/llm/status`

No body. Returns the status object.

### `POST /api/llm/key`

| Body | |
|---|---|
| `provider` | required: `anthropic` or `openai` |
| `key` | the key; empty forgets it |

Errors: 400 for an unknown provider or a key over 512 characters.

### `POST /api/llm/preference`

| Body | |
|---|---|
| `provider` | optional: a provider name, or empty for no preference |
| `model` | optional: a model name, at most 120 characters |

Errors: 400 for an unknown provider or a model name that is too long.

### `POST /api/llm/forget`

No body. Drops every key the caller typed. Keys in the environment are not
touched.

### `POST /api/llm/rules/estimate`

What asking for rules would cost, before anything is sent.

| Body | |
|---|---|
| `schema` | required |

Returns `{calls, per_call, provider, label, model, configured, input_tokens,
output_tokens, dollars, priced, lines}`.

### `POST /api/llm/rules/propose`

Asks the configured model for the form's rules and checks each one against
generated samples. This call sends field names to the provider.

| Body | |
|---|---|
| `schema` | required |
| `sample` | records to check against: 20-5000 |
| `max_spend` | dollars; default 1.0. The call is refused if the estimate is higher |

Returns the proposals with their verdicts, plus `provider` and `label`.

Errors, all 400: no key configured; bad `sample` or `max_spend`; over the
spend limit; a transport failure; a reply that could not be read.

### `POST /api/llm/rules/apply`

Declares the chosen rules on the schema. Each rule is re-read and re-checked
here, so the list cannot be edited on its way back.

| Body | |
|---|---|
| `schema` | required |
| `rules` | required: a non-empty list of proposals |
| `save` | default `true` |
| `parent` | optional: the library entry to save under |

Returns `{schema, summary, applied}`, plus `schema_id` when saved.

Errors: 400 when `rules` is empty, a rule cannot be read, or any rule fails
the check (`those rules do not check out: ...`).

---

## 11. Bots and templates

**Access:** user. These are the Bots panel's routes. They work on the
caller's library and share their code with `/v1` (see
[bot-builder.md](../bot-builder.md) for the template format and the turn
contract).

### `POST /api/bot/templates`

Returns `{templates, starters, schemas, models, semantic_types, bot_llm,
bot_transcribe}`. `templates` and `starters` are template cards; `schemas`
and `models` are the latest 100 of each as `{id, name, created}`, for
linking.

### `POST /api/bot/template`

| Body | |
|---|---|
| `key` | required |

Returns `{template, entry_id}`. If `key` names a bundled starter that is not
in the library yet, returns the starter with `entry_id: null` and
`starter: true`.

Errors: 404 `there is no template called '...'`.

### `POST /api/bot/save`

| Body | |
|---|---|
| `template` | required: the template object |
| `schema_id` | optional: the library schema it came from |

Returns `{template, entry_id}`. Errors: 400 for a template that does not
validate, or a `model_id` that is not in the library.

### `POST /api/bot/delete`

| Body | |
|---|---|
| `key` | required |

Returns `{deleted, entries}`. Errors: 404 for an unknown key.

### `POST /api/bot/starter`

Copies a bundled starter (`address_change`, `document_request`) into the
library.

| Body | |
|---|---|
| `key` | required |

Returns `{template, entry_id}`. Errors: 404 for an unknown starter.

### `POST /api/bot/from_schema`

A draft template with a schema's fields. Not saved.

| Body | |
|---|---|
| `id` | a library schema id, or |
| `schema` | a schema object |

Returns `{template, schema_id}` (`schema_id` is `null` when a schema object
was sent). Errors: 404 for an unknown `id`, 400 for an unreadable schema or
one a template cannot be made from.

### `POST /api/bot/turn`

One turn of the try-it chat. The body and the reply are those of
`POST /v1/bot/turn` ([bot-builder.md §3](../bot-builder.md#3-one-turn)).
The difference: the starters count as available here, even when they are not
in the library yet.

Returns `{messages, actions, expects, form, intent, understood, effect,
conversation, state}`.

Errors carry the `/v1` status (400, 403, 404 or 409) but, because they go
through the `/api` error shape, **not** the `code`.

### `POST /api/bot/transcribe`

A recording from the panel's microphone, as text. Same body as
`/v1/bot/transcribe`: `audio` (base64), `mime` (`audio/...`), optional
`language`.

Returns `{text, via: "speech"}`.

| Status | Cause |
|---|---|
| 409 | the server was not started with `--bot-transcribe` |
| 400 | missing or invalid `audio` or `mime` |
| 502 | the transcription service failed |

---

## 12. API tokens

**Access:** user. Tokens need accounts and a database; with `--no-auth` every
route here answers `409` and says why.

A **token** object is `{id, prefix, name, created, last_used, expires,
model_id, revoked, expired}`. It never contains the secret, which is stored
only as a hash.

### `POST /api/tokens`

The caller's own tokens. No body.

Returns `{tokens, api, cors}`: `api` is `"v1"`, `cors` is the current
`allowed_origins()`.

### `POST /api/tokens/create`

| Body | |
|---|---|
| `name` | optional; cut to 60 characters; default `integration` |
| `days` | optional expiry: 1-3650 |
| `model_id` | optional: pin the token to this library model |

Returns `{token, secret, shown_once: true}`. `secret` (`flr_...`) is the only
time the credential is returned.

| Status | Cause |
|---|---|
| 400 | bad `days`; the account already has 25 tokens |
| 404 | `model_id` is not in the library |
| 409 | no accounts |

### `POST /api/tokens/revoke`

| Body | |
|---|---|
| `id` | required: the token's id |

Returns `{revoked, tokens}`, with `tokens` the caller's list afterwards.
Administrators may revoke anybody's token.

| Status | Cause |
|---|---|
| 403 | `that token belongs to somebody else` |
| 404 | no such token |

---

## 13. Pages, static files and the client

All `GET` (and `HEAD`). Files are served from inside the named directory
only; a path that resolves outside it is a plain-text 404.

| Path | Access | Serves |
|---|---|---|
| `/`, `/index.html` | user | the UI. Signed out, redirects `302` to `/login` |
| `/login` | anonymous | the sign-in page. Redirects to `/` when already signed in (and not due a password change), and always with `--no-auth` |
| `/static/<file>` | anonymous | `fillerai/web/static/`: the UI's script, stylesheet and login page assets |
| `/client`, `/client/` | anonymous | `fillerai/web/static/client/demo.html` |
| `/client/<file>` | anonymous | `fillerai/web/static/client/`: `fillerai.js`, `fillerai-chat.css`, `chat.html`, `demo.html` |

`/static/` can also reach the client files; `/client/` is the documented
path. Everything under `/client/` needs a token to do anything, which is why
it is served signed out.

---

## 14. `/v1` models

Detail, request bodies and examples: [integration.md](../integration.md#the-endpoints).

| Method | Path | Access | Body | Returns | Refusals (`code`) |
|---|---|---|---|---|---|
| `GET` | `/v1/health` | anonymous | — | `{service, version, api, ok}` | — |
| `GET` | `/v1/models` | token | — | `{models, count}` (up to 200) | — |
| `GET` | `/v1/models/{id}` | token | — | `{model, threshold, ask_first, screens, fields}` | `out_of_scope` 403, `not_found` 404, `not_a_model` 400 |
| `POST` | `/v1/models/{id}/suggest` | token | `observed`, `threshold`, `fields` | `{model_id, threshold, given, suggestions, values, offered, considered}` | as above, plus `bad_observed`, `unknown_field`, `bad_threshold`, `bad_fields` 400, `too_large` 413 (over 2000 observed fields) |
| `POST` | `/v1/models/{id}/fill` | token | `observed`, `threshold` | `{model_id, threshold, record, filled, offered}` | as for `suggest`, less `bad_fields` |
| `POST` | `/v1/models/{id}/batch` | token | `records` (1-500), `threshold` | `{model_id, threshold, count, offered, results}` | as for `fill`, plus `bad_records` 400, `too_large` 413 |

`{id}` is a library model id: 1-80 characters of letters, digits, `.`, `_`
and `-`. A token pinned to a model sees only that model in `/v1/models`, and
gets `out_of_scope` for any other.

---

## 15. `/v1` bot

Detail: [bot-builder.md §6](../bot-builder.md#6-the-endpoints) and the
sections it points to.

| Method | Path | Access | Body | Returns | Refusals (`code`) |
|---|---|---|---|---|---|
| `GET` | `/v1/templates` | token | — | `{templates, count}` (cards) | — |
| `POST` | `/v1/templates` | unpinned token | the template, bare or as `{template}` | `{template, entry_id}` | `out_of_scope` 403, `bad_template` 400, `not_found`/`not_a_model` for a bad `model_id` |
| `GET` | `/v1/templates/{key}` | token | — | `{template, entry_id}` | `unknown_template` 404, `out_of_scope` 403 |
| `POST` | `/v1/templates/{key}/delete` | unpinned token | — | `{deleted, entries}` | `out_of_scope` 403, `unknown_template` 404 |
| `POST` | `/v1/bot/turn` | token | `input`, `state`, `context` | the turn reply ([§3.2](../bot-builder.md#32-the-reply)) | `bad_input`, `bad_state` 400; `template_not_allowed`, `out_of_scope` 403; `unknown_template` 404; `stale_action`, `no_templates` 409; `too_large` 413 (text over the limit) |
| `POST` | `/v1/bot/transcribe` | token | `audio`, `mime`, `language` | `{text, via}` | `bad_input` 400, `transcription_off` 409, `transcription_failed` 502 |

`{key}` is 1-64 characters: a lowercase letter, then lowercase letters,
digits and `_`. A token pinned to a model reaches only templates linked to
that model and cannot create or delete any.

---

## 16. Sample application

Northwind Mutual, the sample customer portal. It is a separate server
(`fillerai/sampleapp/app.py`) that talks to FillerAI only over `/v1`.
`fillerai serve` starts it on port 8100 (`--sample-port`, `--sample-user`,
`--no-sample-app`), issuing it a token named `Sample application` for the
first administrator or the named user. It can also run alone:
`python -m fillerai.sampleapp --fillerai URL --token flr_...`.

This section reflects version 0.15.1. The sample application is being
changed, so check the code before relying on the detail.

The page draws one form per template from `/api/templates` when it loads and
shows one at a time: the one the chat conversation is about, or the one
picked from the menu to fill and submit by hand. The open form is kept in
the address as `#form-<template>`, so a reload or a link opens the same one.
All of this is in the page; it adds no route.

It has no accounts, cookies, CSRF or CORS: it is one demo customer on
localhost. The token stays on this server and never reaches the browser.
Errors are `{"error": "..."}`. An error passed on from FillerAI keeps
FillerAI's status and body, `code` included. When FillerAI cannot be reached
the reply is `502` with `code: unreachable`.

| Method | Path | Does |
|---|---|---|
| `GET` | `/` | the portal page (`static/index.html`, one form open at a time, `#form-<template>`); other files under `static/` by name |
| `GET` | `/api/me` | the customer record and past requests, plus `server_speech` and `fillerai_page` |
| `GET` | `/api/templates` | every FillerAI template in full, as `{templates}`, fetched from `/v1/templates` on each call |
| `GET` | `/fillerai.js`, `/fillerai-chat.css` | FillerAI's client files, fetched through this server; 502 when FillerAI is down |
| `GET` | `/favicon.ico` | `204`, empty |
| `POST` | `/api/chat` | forwards `input`, `state` and `context.template` to `/v1/bot/turn`, replacing `context.current` with the customer record |
| `POST` | `/api/submit/{key}` | body `{values}`: checks them against the template and the portal's own rules, updates the record, returns `{reference, customer}` |
| `POST` | `/api/transcribe` | forwards `audio`, `mime`, `language` to `/v1/bot/transcribe`; only with `--server-speech`, otherwise 404 |

Refusals from the portal itself:

| Status | Cause |
|---|---|
| 400 | body not JSON or not an object; a required field missing; a value that fails a rule (option, ZIP code, state, email); nothing changed |
| 403 | a policy number that is not the customer's |
| 404 | unknown path; a `{key}` the portal will not accept, or one FillerAI does not have |
| 413 | a body over 64 KB (8 MB for `/api/transcribe`) |
