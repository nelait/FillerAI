# AIrForms in a container, for Railway or anywhere else that runs one.
# AIrForms itself is standard library only: copy the package and run it.
# Railway finds this file and builds from it on its own.
FROM python:3.12-slim

# Logs straight to the platform, so the first-run lines are seen at once.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FILLERAI_HOME=/data

WORKDIR /app
# The one optional extra: the Postgres driver, so pointing
# FILLERAI_DATABASE_URL at a postgresql:// database is all it takes. Unused
# while the database is the default SQLite file.
RUN pip install --no-cache-dir "psycopg[binary]>=3.1"
COPY fillerai ./fillerai
COPY examples ./examples
# The /docs pages render these; the README is the docs home page.
COPY docs ./docs
COPY README.md ./

# With the default SQLite database, accounts and the library live in
# $FILLERAI_HOME: on Railway attach a volume at /data, or they are lost at
# every deploy. With FILLERAI_DATABASE_URL set to Postgres they live there
# instead and no volume is needed. (No VOLUME line: Railway refuses one.)

# $PORT is read by `serve` itself. 0.0.0.0 so the platform's proxy can reach
# it; accounts stay on (--no-auth is refused off loopback). The sample app is
# off: it needs a second port and Railway gives a service one.
CMD ["python", "-m", "fillerai", "serve", "--host", "0.0.0.0", "--trust-proxy", "--no-sample-app"]
