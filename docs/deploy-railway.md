# Deploying to Railway

The repository deploys to [Railway](https://railway.com) as it is. Railway
finds the `Dockerfile` (and `railway.json`), builds it, and runs:

```
python -m fillerai serve --host 0.0.0.0 --trust-proxy
```

- **Port.** `serve` reads `$PORT`, which Railway sets.
- **Accounts stay on.** `--no-auth` is refused anywhere but localhost, and a
  public address is not localhost.
- **Data, two ways.** By default accounts, the library and the SQLite
  database live in `FILLERAI_HOME`, which the image sets to `/data`; without a
  volume mounted there they are wiped at every deploy, and the server says so
  in its log. Or set `FILLERAI_DATABASE_URL` to a Railway Postgres database
  and everything lives there instead, with no volume. The image carries the
  Postgres driver (`psycopg`), so that variable is all it takes.
- **First administrator.** `admin`, with the password in
  `FILLERAI_ADMIN_PASSWORD` (at least 8 characters). Without it, a generated
  one is printed once in the deploy log and must be changed at first sign-in.
- **Proxy.** `--trust-proxy` takes the visitor's address and the https scheme
  from Railway's proxy headers, so the session cookie is marked `Secure` and
  the docs access-code limit counts each visitor rather than the proxy.
- **The sample application is at `/sample/`** on the same address (the
  "Sample app" link in the app's left rail). It runs inside the same
  service, and only signed-in people can open it, since its chat uses an
  administrator's token. For a public demo, add the variable `FILLERAI_SAMPLE_PUBLIC` = `1`.
  Its forms are the admin's bot templates, so add the starters on the Bots
  tab first.
- **One replica.** SQLite on a volume is one process's database, and even with
  Postgres a training run's live log is held in the process that runs it.

## Steps

1. In Railway, **New Project → Deploy from GitHub repo**, and pick
   `nelait/fillerai`. It starts building from the Dockerfile.
2. Choose where the data lives, one of:
   - **Postgres (recommended).** On the project canvas, **+ New → Database →
     Add PostgreSQL**. Then in the AIrForms service, **Variables → New
     Variable**: `FILLERAI_DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
     (Railway fills in the reference).
   - **SQLite on a volume.** Right-click the AIrForms service on the canvas →
     **Attach volume**, and mount it at **`/data`**.
3. **Variables → New Variable**: `FILLERAI_ADMIN_PASSWORD` = a password of at
   least 8 characters. Optional: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for
   the language-model features.
4. **Settings → Networking → Generate Domain**. Railway asks for the port only
   if it cannot tell; the app listens on whatever `$PORT` it was given.
5. Open the domain, choose **Sign in**, and use `admin` with the password
   from step 3. Then set the docs access code in **Settings →
   Documentation access** if you want `/docs` open.

The variables must be set before the first start that creates the
administrator. If it started without one, the generated password is in the
deploy log, under "No accounts yet".

## Backing up

With Postgres, Railway's database backups cover everything. With SQLite, the
volume holds everything, and Railway's volume backups (service → Volume →
Backups) copy it. To run a command against it, `railway ssh` into the
service from the Railway CLI, then for example `python -m fillerai db status`.
