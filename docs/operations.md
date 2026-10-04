# Operations

Installing, running, configuring, backing up and troubleshooting AIrForms.
For what each command does in the modelling process see
[process.md](process.md); for every option of every command see
[reference/cli.md](reference/cli.md).

Checked against the code at version 0.15.0.

**Contents**

1. [Requirements](#1-requirements)
2. [Installing](#2-installing)
3. [Starting the server](#3-starting-the-server)
4. [Configuration](#4-configuration)
5. [Where the data is, and backing it up](#5-where-the-data-is-and-backing-it-up)
6. [Accounts and tokens from a shell](#6-accounts-and-tokens-from-a-shell)
7. [Upgrading](#7-upgrading)
8. [The sample application](#8-the-sample-application)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Requirements

- **Python 3.10 or later.** Nothing else: `dependencies = []`.
- A modern browser for the UI and the chat (the microphone needs `https://`
  or `http://localhost`).
- Optional: an Anthropic or OpenAI API key for the language-model features.
  None of the core needs one.

## 2. Installing

From a checkout, nothing needs installing:

```sh
cd fillerai
python -m fillerai --version
```

Or install it, which adds a `fillerai` command and carries the UI, the
JavaScript client, the starter templates and the sample application as
package data:

```sh
pip install .            # or: pip install -e . while developing
fillerai --version
```

Where nothing may be installed, copying the `fillerai/` directory and running
`python -m fillerai` is enough.

## 3. Starting the server

```sh
python -m fillerai serve
```

The first start with an empty database creates an administrator and prints
the password once:

```
  No accounts yet, so one administrator was created:
    username: admin
    password: djfg-y3gj-dqpg-jegu
    (shown once; you will be asked to change it when you sign in)

AIrForms on http://localhost:8000/  (app: http://localhost:8000/app, docs: http://localhost:8000/docs)
  database: sqlite:///…/.fillerai/fillerai.db
  accounts: on, 1 user(s)
  integration API: http://localhost:8000/v1  (browsers: *)
  JavaScript client: http://localhost:8000/client/fillerai.js
  chat demo: http://localhost:8000/client/chat.html  (bot reads phrases on this machine)
  sample application: http://localhost:8100/  (its forms are the bot templates, as admin)
  press Ctrl-C to stop
```

`/` is the public product page, with a **Sign in** link; the app itself is at
`/app` (the old `/index.html` redirects there), and `--open` opens `/app`.
`/docs` serves the documents in `docs/` as HTML pages, behind an access code an
administrator sets in **Settings → Documentation access**. Until a code is set
the docs are closed to everybody.

Set `FILLERAI_ADMIN_PASSWORD` beforehand to choose that password instead, or
create accounts from a shell first (§6). If a `.fillerai` file library
already exists, its contents are copied into the first administrator's
library, ids and lineage intact.

Common variations:

| Want | Run |
|---|---|
| one person, no sign-in | `serve --no-auth` (localhost only) |
| a different port | `serve -p 9000` |
| log every request | `serve -v` |
| open a browser | `serve --open` |
| keep the data elsewhere | `serve --library /srv/fillerai` or `--database sqlite:///srv/fillerai.db` |
| no sample application | `serve --no-sample-app` |
| a model reads chat phrases | `serve --bot-llm` with a key set |
| record and transcribe speech on the server | `serve --bot-transcribe` with an OpenAI key |
| let a browser app on another origin call `/v1` | `serve --cors-origin https://app.example` (repeatable) |

The server is a single process with a thread per request. Stop it with
Ctrl-C, which also stops the sample application and closes the database.

To run it as a service, any supervisor that runs a command and restarts it
will do; for example a systemd unit with
`ExecStart=/usr/bin/python3 -m fillerai serve --library /srv/fillerai` and
`WorkingDirectory` set to the checkout. Put the API keys in the unit's
`Environment=` or an `EnvironmentFile` readable only by the service user.

## 4. Configuration

Command-line options win over environment variables, which win over the
defaults.

| Variable | Used by | Meaning |
|---|---|---|
| `FILLERAI_HOME` | CLI, `serve` | The library directory. Default `./.fillerai`. |
| `FILLERAI_DATABASE_URL` | `serve`, `users`, `tokens`, `db` | `sqlite://<path>`. Default `fillerai.db` in the library directory. |
| `FILLERAI_ADMIN_PASSWORD` | first `serve` | The first administrator's password instead of a generated one. |
| `PORT` | `serve` | The port, as hosting platforms set it. Default 8000. |
| `FILLERAI_TRUST_PROXY` | `serve` | `1` is the same as `--trust-proxy`: take the visitor's address and https from the proxy's headers. Only behind such a proxy. |
| `FILLERAI_BOT_LLM` | `serve` | `1` is the same as `--bot-llm`. |
| `FILLERAI_BOT_TRANSCRIBE` | `serve` | `1` is the same as `--bot-transcribe`. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | LLM features | The provider's key. |
| `FILLERAI_LLM_KEY` | LLM features | A key for whichever provider; its prefix says which. |
| `FILLERAI_LLM_PROVIDER` | LLM features | `anthropic` or `openai`, when it cannot be inferred. |
| `FILLERAI_LLM_MODEL` | LLM features | A model name other than the per-task default. |
| `FILLERAI_LLM_BASE_URL` | LLM features | A different endpoint, such as an approved gateway. |
| `FILLERAI_TRANSCRIBE_BASE_URL`, `FILLERAI_TRANSCRIBE_MODEL` | `--bot-transcribe` | Where and with what model speech is transcribed. |
| `FILLERAI_URL`, `FILLERAI_TOKEN` | `bot chat`, the sample application run on its own | Which AIrForms to talk to, and the API token. |
| `FILLERAI_LLM_LIVE` | `livetests/` only | `1` lets the live acceptance tests spend money. |

`python -m fillerai llm status` prints which provider, model and key the
language-model features would use and what decided it, without printing the
key.

Deploying to Railway, with a Dockerfile in the repository, is in
[deploy-railway.md](deploy-railway.md).

## 5. Where the data is, and backing it up

With the defaults, everything is under one directory:

```
.fillerai/
  fillerai.db          accounts, sessions, API tokens, every library entry
  fillerai.db-wal      SQLite's write-ahead log (part of the database)
  fillerai.db-shm      and its index
  sample-app/          the sample application's demo customer
  schema/ dataset/ …   a file library, if the CLI has written one here
```

The CLI's own commands (`extract`, `generate`, `train`, `library …`) write
to the **file library** unless told otherwise; the server writes to the
**database**. `fillerai db import --user NAME` copies a file library into a
user's database library, and `fillerai db status` says what is where.

**Backing up the database.** While the server is running, use SQLite's
online backup rather than copying the file, because the WAL may hold
committed writes not yet in `fillerai.db`:

```sh
sqlite3 .fillerai/fillerai.db ".backup '/backups/fillerai-$(date +%F).db'"
```

With the server stopped, copying `fillerai.db` alone is enough (the WAL is
folded in on a clean close). Restore by stopping the server and putting the
copy back as `fillerai.db`, removing any stale `-wal` and `-shm` files.

The file library is plain files; copy the directory.

**Keys typed into Settings are not in any backup**, by design; they are
forgotten on restart.

## 6. Accounts and tokens from a shell

These work on the database directly, so they are also the way back in when
nobody can sign in.

```sh
python -m fillerai users list
python -m fillerai users add alice --admin          # prints a generated password
python -m fillerai users passwd admin               # a new password; ends their sessions
python -m fillerai users disable bob                # also revokes bob's API tokens
python -m fillerai tokens add alice --name "Claims portal" --days 90
python -m fillerai tokens list
python -m fillerai tokens revoke "Claims portal"      # an id, its flr_ prefix, or its name
```

The exact options of each are in [reference/cli.md](reference/cli.md). The
same things are on the admin cards in Settings, and every user can issue and
revoke their own tokens there.

## 7. Upgrading

1. Stop the server and back up the database (§5).
2. Replace the code (`git pull`, or `pip install` the new version).
3. Start the server. Database migrations run automatically on connect, in
   order, once each; `fillerai db status` prints the schema version.

Migrations only ever add a step; a shipped step is never edited, so a
database from any earlier version upgrades in place. Library entries are
immutable and carry the schema version they were written with; loading a
schema whose `schema_version` has a different major number is refused with a message naming both.

Read the top of the README and the merged pull requests for what changed.
There is no separate changelog file.

## 8. The sample application

`serve` starts Northwind Mutual, a demo customer portal, on port 8100, and
gives it an API token called "Sample application" for the first
administrator (replacing the previous one on each start). Its forms are that
user's bot templates, so add the starters on the Bots tab or with
`fillerai bot add --starter address_change` first. `--sample-user NAME`
shows another user's templates, `--sample-port` moves it and
`--no-sample-app` leaves it off.

One form is open at a time: the chat opens the one the conversation is
about, and the menu at the top opens any of them to fill in and submit by
hand (`http://localhost:8100/#form-address_change` opens that one directly).

It can run on its own against any AIrForms:

```sh
python -m fillerai.sampleapp --fillerai http://localhost:8000 --token flr_...
```

It is a demo with no sign-in of its own; see [security.md](security.md) §9.
Its own documentation is `fillerai/sampleapp/README.md`.

## 9. Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `--no-auth … is only allowed on localhost` and exit 1 | `--no-auth` with `--host` other than loopback | Drop `--no-auth`, or keep `--host 127.0.0.1`. |
| `sample application: not started, port 8100 is taken` | Another program, or another AIrForms, has the port | `--sample-port 8101`, or `--no-sample-app`. The UI still starts. |
| `sample application: not started, there is no administrator` / `no user` | `--sample-user` names nobody, or the database has no admin | Create the user, or drop `--sample-user`. |
| The sample application shows no forms | The token's user has no bot templates | Add the starters on the Bots tab, then reload. |
| The sample application shows no form, only "What can we help with?" | None is open yet; that is the start | Ask for one in the chat, or pick one from the menu at the top. |
| Nobody can sign in | Lost password | `fillerai users passwd admin` from a shell on the server. |
| "too many failed attempts; try again in N minute(s)" | 6 wrong passwords | Wait 15 minutes, or set a new password with `users passwd`. |
| A browser app gets a CORS error from `/v1` | Running `--no-auth`, where no origin is allowed | `--cors-origin https://your-app`, or turn accounts on. |
| `/v1` answers `401` | Missing, revoked or expired token, or its user is disabled | Issue a new token. `/v1` never uses the UI's sign-in. |
| `/v1/bot/turn` answers `409 stale_action` | An action id not offered on the previous turn, usually a double click | Nothing; the first click already counted. |
| The chat microphone hears nothing, while other apps hear you | The browser picked another input device (for example an iPhone via Continuity on a Mac) | Choose the right microphone in the browser's site settings or the OS sound settings. |
| The microphone fails with a network error | The browser's speech service is blocked on this network | `serve --bot-transcribe` with an OpenAI key, which records in the page and transcribes on the server. |
| Propose rules says the answer was cut off by the token limit | Very large form on an older version | Fixed in 0.10.1: the budget is sized from the form and split across calls of 60 fields. Upgrade. |
| Models trained in the UI do not appear in the Library | Fixed in 0.6.2 | Upgrade. |
| The test suite occasionally fails in `TestSeparateLibraries` | A known race in the in-memory SQLite backend used by tests | Re-run; tracked in [pending.md](pending.md) §2.1. Users are not affected. |

For a request-by-request view, start the server with `-v`.
