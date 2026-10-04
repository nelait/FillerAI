# AIrForms in a container, for Railway or anywhere else that runs one.
# Standard library only, so there is nothing to install: copy the package and
# run it. Railway finds this file and builds from it on its own.
FROM python:3.12-slim

# Logs straight to the platform, so the first-run lines are seen at once.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FILLERAI_HOME=/data

WORKDIR /app
COPY fillerai ./fillerai
COPY examples ./examples
# The /docs pages render these; the README is the docs home page.
COPY docs ./docs
COPY README.md ./

# Accounts and the library (the SQLite database included) live in
# $FILLERAI_HOME. On Railway, attach a volume mounted at /data or they are
# lost at every deploy. (No VOLUME line: Railway refuses one.)

# $PORT is read by `serve` itself. 0.0.0.0 so the platform's proxy can reach
# it; accounts stay on (--no-auth is refused off loopback). The sample app is
# off: it needs a second port and Railway gives a service one.
CMD ["python", "-m", "fillerai", "serve", "--host", "0.0.0.0", "--trust-proxy", "--no-sample-app"]
