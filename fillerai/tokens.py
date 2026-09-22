"""Credentials for an application, as opposed to a person.

A browser signing in gets a cookie and a CSRF token, which is exactly right
for a browser and exactly wrong for a claims system calling FillerAI from a
server room. What an application needs is one string it can put in a config
file, that never expires by walking away from the keyboard, that a person can
revoke without changing their own password, and that is not attached to a
request some other page can cause the browser to send.

So: **an API token is a bearer credential, and the REST surface takes nothing
else.** Not the cookie - deliberately, because a surface that honours a cookie
is a surface that can be driven from any page the user has open. The cookie
belongs to :mod:`fillerai.web.server`'s own ``/api`` endpoints and stops
there.

**A token is shown once and stored as a hash.** The hash is plain SHA-256
rather than the scrypt used for passwords, and that is a considered
difference, not an oversight: a password is a short string a person chose, so
the cost of hashing is what stands between a stolen table and a working
login. A token's secret is 32 bytes from :mod:`secrets`, which no amount of
guessing reaches, and it is verified on every single API call - a 45ms hash
would make the integration surface useless for the thing it exists for.

**The id travels with the secret** (``flr_<id>_<secret>``), so verifying is
one indexed lookup and one constant-time compare rather than a scan of every
token in the table hashing each one in turn.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any

from .db import Database

#: What a token looks like on the wire. The prefix is there so that a token
#: pasted into a chat window, a log or a repository is recognisable as one -
#: which is what makes automated secret scanning possible at all.
PREFIX = "flr"
_ID_CHARS = 12
_SECRET_BYTES = 24

#: A name is for the person reading the list six months later; it is not
#: part of the credential.
MAX_NAME = 60
MAX_TOKENS_PER_USER = 25

_TOKEN = re.compile(r"^flr_([0-9a-f]{%d})_([A-Za-z0-9_-]{16,64})$" % _ID_CHARS)


class TokenError(Exception):
    """Something to tell the person holding the token, or making one."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class ApiToken:
    """One issued credential, as the person who issued it sees it.

    Never the secret: it is not stored, so there is nothing here to leak. The
    prefix is the first few characters of the token as issued, which is what
    lets somebody match a row in this list against the string in their
    application's configuration without either of them being the credential.
    """

    id: str
    user_id: str
    name: str
    created: str
    last_used: str | None = None
    expires: str | None = None
    #: The one model this token may ask about, or None for the whole library.
    model_id: str | None = None
    revoked: bool = False

    @property
    def prefix(self) -> str:
        return f"{PREFIX}_{self.id}"

    def expired(self, now: dt.datetime | None = None) -> bool:
        if not self.expires:
            return False
        return _parse(self.expires) <= (now or dt.datetime.now().astimezone())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prefix": self.prefix,
            "name": self.name,
            "created": self.created,
            "last_used": self.last_used,
            "expires": self.expires,
            "model_id": self.model_id,
            "revoked": self.revoked,
            "expired": self.expired(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ApiToken":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            name=str(row["name"]),
            created=str(row["created"]),
            last_used=row.get("last_used") or None,
            expires=row.get("expires") or None,
            model_id=row.get("model_id") or None,
            revoked=bool(row.get("revoked")),
        )


class Tokens:
    """Issue, list, verify and revoke API tokens, over a :class:`Database`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- issuing ------------------------------------------------------------

    def issue(self, user_id: str, name: str = "", *, days: int | None = None,
              model_id: str | None = None) -> tuple[ApiToken, str]:
        """Make a token. Returns the record and the secret, shown once.

        The secret is the only thing the caller will ever be able to use and
        the only thing this class refuses to remember, so a caller that drops
        it has to issue another one. That is the intended trade.
        """
        label = (str(name or "").strip() or "integration")[:MAX_NAME]
        if self.count(user_id) >= MAX_TOKENS_PER_USER:
            raise TokenError(
                f"that account already has {MAX_TOKENS_PER_USER} tokens; "
                f"revoke one before making another")
        if days is not None and days <= 0:
            raise TokenError("an expiry is a number of days in the future")

        token_id = secrets.token_hex(_ID_CHARS // 2)
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        now = dt.datetime.now().astimezone()
        expires = _stamp(now + dt.timedelta(days=days)) if days else None

        record = ApiToken(
            id=token_id, user_id=user_id, name=label, created=_stamp(now),
            expires=expires, model_id=model_id or None,
        )
        self.db.execute(
            "INSERT INTO api_tokens (id, user_id, name, secret_hash, created, "
            "last_used, expires, model_id, revoked) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 0)",
            (record.id, user_id, record.name, _digest(secret), record.created,
             expires, record.model_id),
        )
        return record, f"{PREFIX}_{token_id}_{secret}"

    # -- reading ------------------------------------------------------------

    def count(self, user_id: str) -> int:
        return self.db.count(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id = ? AND revoked = 0",
            (user_id,))

    def list(self, user_id: str | None = None,
             include_revoked: bool = False) -> list[ApiToken]:
        sql = "SELECT * FROM api_tokens"
        where, params = [], []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if not include_revoked:
            where.append("revoked = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC"
        return [ApiToken.from_row(row) for row in self.db.query(sql, params)]

    def get(self, token_id: str) -> ApiToken:
        row = self.db.one("SELECT * FROM api_tokens WHERE id = ?", (token_id,))
        if row is None:
            raise TokenError("there is no such token", status=404)
        return ApiToken.from_row(row)

    def find(self, user_id: str, name: str) -> ApiToken | None:
        for token in self.list(user_id):
            if token.name == name:
                return token
        return None

    # -- verifying ----------------------------------------------------------

    def verify(self, presented: str) -> ApiToken:
        """The token a string names, or :class:`TokenError` saying why not.

        Every failure carries the same message. A caller getting a token
        wrong is not owed the difference between "no such token", "wrong
        secret" and "revoked yesterday" - that difference is a way of asking
        this endpoint which tokens exist.
        """
        wrong = TokenError("that API token is not valid", status=401)
        match = _TOKEN.match(str(presented or "").strip())
        if match is None:
            raise wrong
        token_id, secret = match.group(1), match.group(2)

        row = self.db.one("SELECT * FROM api_tokens WHERE id = ?", (token_id,))
        if row is None:
            raise wrong
        if not hmac.compare_digest(str(row["secret_hash"]), _digest(secret)):
            raise wrong

        token = ApiToken.from_row(row)
        if token.revoked:
            raise wrong
        if token.expired():
            raise TokenError("that API token has expired", status=401)
        return token

    def touch(self, token_id: str) -> None:
        """Note that a token was just used, so a stale one can be spotted.

        Best effort and deliberately not part of :meth:`verify`: a write per
        request is the wrong shape for something an application calls in a
        loop, so the caller decides when it is worth one.
        """
        self.db.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?",
                        (_now(), token_id))

    # -- ending -------------------------------------------------------------

    def revoke(self, token_id: str) -> ApiToken:
        """Turn a token off for good. Kept as a row, so the list still tells
        the story of what was issued and when it stopped."""
        token = self.get(token_id)
        self.db.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (token_id,))
        token.revoked = True
        return token

    def revoke_all(self, user_id: str) -> int:
        return self.db.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0",
            (user_id,))

    def forget(self, token_id: str) -> None:
        self.db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> str:
    return _stamp(dt.datetime.now().astimezone())


def _stamp(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(text: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return dt.datetime.fromtimestamp(0).astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment
