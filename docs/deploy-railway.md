# Deploying to Railway

The repository deploys to [Railway](https://railway.com) as it is. Railway
finds the `Dockerfile` (and `railway.json`), builds it, and runs:

```
python -m fillerai serve --host 0.0.0.0 --trust-proxy --no-sample-app
```

- **Port.** `serve` reads `$PORT`, which Railway sets.
- **Accounts stay on.** `--no-auth` is refused anywhere but localhost, and a
  public address is not localhost.
- **Data.** Accounts, the library and the SQLite database live in
  `FILLERAI_HOME`, which the image sets to `/data`. Without a volume mounted
  there they are wiped at every deploy, and the server says so in its log.
- **First administrator.** `admin`, with the password in
  `FILLERAI_ADMIN_PASSWORD` (at least 8 characters). Without it, a generated
  one is printed once in the deploy log and must be changed at first sign-in.
- **Proxy.** `--trust-proxy` takes the visitor's address and the https scheme
  from Railway's proxy headers, so the session cookie is marked `Secure` and
  the docs access-code limit counts each visitor rather than the proxy.
- **The sample application is off.** It needs a second port and a Railway
  service exposes one. Run it locally, or as a second Railway service from
  the same repository with the start command
  `python -m fillerai.sampleapp --host 0.0.0.0` (it reads `$PORT` too) and the
  variables `FILLERAI_URL` (this service's https address) and
  `FILLERAI_TOKEN` (an API token made in Settings).
- **One replica.** SQLite on a volume is one process's database.

## Steps

1. In Railway, **New Project → Deploy from GitHub repo**, and pick
   `nelait/fillerai`. It starts building from the Dockerfile.
2. Open the service, then **Settings → Volumes** (or right-click the service
   on the canvas → **Attach volume**), and mount it at **`/data`**.
3. **Variables → New Variable**: `FILLERAI_ADMIN_PASSWORD` = a password of at
   least 8 characters. Optional: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for
   the language-model features.
4. **Settings → Networking → Generate Domain**. Railway asks for the port only
   if it cannot tell; the app listens on whatever `$PORT` it was given.
5. Open the domain, choose **Sign in**, and use `admin` with the password
   from step 3. Then set the docs access code in **Settings** if you want
   `/docs` open.

The variables must be set before the first start that creates the
administrator. If it started without one, the generated password is in the
deploy log, under "No accounts yet".

## Backing up

The volume holds everything. Railway's volume backups (service → Volume →
Backups) copy it. To run a command against it, `railway ssh` into the
service from the Railway CLI, then for example `python -m fillerai db status`.
