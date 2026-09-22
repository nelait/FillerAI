"""Who is using this, and what they are allowed to do.

FillerAI started as a tool one person ran on their own machine, where "who
are you" had one answer and asking was rude. It is now a thing several people
share, and the two questions that follow are unavoidable: whose library is
this, and who is allowed to hand out accounts.

The answers here are deliberately ordinary. A user has a name, a role, and a
password hashed with something slow. A login creates a row; the browser gets
a cookie naming that row and nothing else. An admin can make accounts and
turn them off. Credentials for an *application* are a different thing with
different rules and live in :mod:`fillerai.tokens`; what they share with an
account is that turning the account off turns them off too. There is nothing clever, and that is the point: the clever
part of this project is the form model, and an authentication layer that
surprises anybody has already failed.

**Passwords are hashed with scrypt** (``hashlib.scrypt``, standard library)
at the parameters usually called interactive - about 45ms and 16MB per
attempt, which is nothing to a person logging in and a great deal to somebody
working through a stolen table. PBKDF2 is kept as a fallback for a Python
built against an OpenSSL without scrypt, and both formats are recognised on
the way back in, so a hash written by one build still verifies under another.

**Sessions live in the database, not in the cookie.** The cookie carries a
random session id and a signature over it. The signature is not what makes
the session safe - the id is random and is looked up server-side either way -
it is what lets a forged or corrupted cookie be thrown out without touching
the database, and what stops a stale cookie from a rebuilt database matching
anything. Signing out deletes the row, so it takes effect everywhere at once,
which a self-contained token cannot promise.

**The last administrator cannot be removed.** Not disabled, not demoted, not
deleted. A shared tool that can be locked out of itself by one careless click
is a support call, and the check costs one query.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database

#: The two roles. Kept at two on purpose: every third role anybody proposes
#: turns out to be a permission, and permissions belong to whatever they
#: guard, not to a list here.
ADMIN = "admin"
USER = "user"
ROLES = (ADMIN, USER)

#: A username becomes part of a URL, a filename in an export and a column in
#: a listing, so it is deliberately narrow.
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

MIN_PASSWORD = 8
MAX_PASSWORD = 1024  # a hash is cheap; hashing a 10MB paste is not

#: How long a session lasts without use, and the longest it can live at all.
#: Sliding, because the alternative is being signed out mid-run.
IDLE_HOURS = 12
MAX_HOURS = 24 * 7

#: Failed attempts before a pause, and how long the pause is. Not an account
#: lock in the punitive sense - it clears itself - just enough that guessing
#: at a password over the network stops being arithmetic.
MAX_FAILURES = 6
LOCKOUT_MINUTES = 15

COOKIE = "fillerai_session"
SECRET_SETTING = "session_secret"

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_PBKDF2_ROUNDS = 600_000


class AuthError(Exception):
    """Something a person should be told, in the words they should hear it."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ----------------------------------------------------------------------
# passwords
# ----------------------------------------------------------------------


def hash_password(password: str) -> str:
    """A hash that carries its own parameters, so they can change later."""
    salt = secrets.token_bytes(16)
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
        return "$".join((
            "scrypt", str(_SCRYPT["n"]), str(_SCRYPT["r"]), str(_SCRYPT["p"]),
            _b64(salt), _b64(digest),
        ))
    except (ValueError, AttributeError):  # pragma: no cover - OpenSSL without scrypt
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                     _PBKDF2_ROUNDS)
        return "$".join(("pbkdf2_sha256", str(_PBKDF2_ROUNDS),
                         _b64(salt), _b64(digest)))


def verify_password(stored: str, password: str) -> bool:
    """Check a password against a stored hash, in constant time."""
    try:
        parts = (stored or "").split("$")
        if parts[0] == "scrypt":
            _, n, r, p, salt, digest = parts
            candidate = hashlib.scrypt(
                password.encode("utf-8"), salt=_unb64(salt),
                n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest)))
        elif parts[0] == "pbkdf2_sha256":
            _, rounds, salt, digest = parts
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), _unb64(salt), int(rounds),
                dklen=len(_unb64(digest)))
        else:
            return False
    except (ValueError, TypeError, IndexError):
        # A malformed hash is a failed login, not an exception out of a login
        # form. It cannot be anybody's password either way.
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


def check_password_quality(password: str, username: str = "") -> None:
    """Refuse the passwords that are certainly a mistake, and no more.

    Length and "not your own name" are the two rules that catch the real
    cases. A composition rule on top of them - a digit, a symbol, a capital -
    reliably produces ``Password1!`` and nothing else.
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f"a password needs at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AuthError("that password is implausibly long")
    if username and password.strip().lower() == username.strip().lower():
        raise AuthError("a password cannot be the username")


def suggest_password(words: int = 4) -> str:
    """A password to hand somebody, readable enough to type once."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(words))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ----------------------------------------------------------------------
# what a user is
# ----------------------------------------------------------------------


@dataclass
class User:
    id: str
    username: str
    display_name: str
    role: str
    active: bool
    created: str
    last_login: str | None = None
    must_change: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    def to_dict(self) -> dict[str, Any]:
        """What the UI is told. Never the hash, never the failure count."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "active": self.active,
            "created": self.created,
            "last_login": self.last_login,
            "must_change": self.must_change,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "User":
        return cls(
            id=str(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            active=bool(row["active"]),
            created=str(row["created"]),
            last_login=row.get("last_login") or None,
            must_change=bool(row.get("must_change")),
        )


@dataclass
class Session:
    id: str
    user_id: str
    csrf: str
    created: str
    last_seen: str
    expires: str


# ----------------------------------------------------------------------
# the thing that does the work
# ----------------------------------------------------------------------


class Auth:
    """Accounts and sessions, over a :class:`~fillerai.db.Database`."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._secret = self._load_secret()

    # -- the signing secret -------------------------------------------------

    def _load_secret(self) -> bytes:
        """The key cookies are signed with, made once and kept.

        In the database rather than in a file beside it, so that copying the
        database copies everything needed to keep existing sessions valid,
        and so that deleting it invalidates them - which is what a person who
        deletes a database expects to have happened.
        """
        stored = self.db.setting(SECRET_SETTING)
        if stored:
            return _unb64(stored)
        secret = secrets.token_bytes(32)
        self.db.remember(SECRET_SETTING, _b64(secret))
        return secret

    # -- accounts -----------------------------------------------------------

    def count(self) -> int:
        return self.db.count("SELECT COUNT(*) FROM users")

    def admins(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role = ?"
        params: list[Any] = [ADMIN]
        if active_only:
            sql += " AND active = 1"
        return self.db.count(sql, params)

    def users(self) -> list[User]:
        return [User.from_row(row) for row in self.db.query(
            "SELECT * FROM users ORDER BY username")]

    def get(self, user_id: str) -> User:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise AuthError("there is no such user", status=404)
        return User.from_row(row)

    def find(self, username: str) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE username = ?",
                          (str(username or "").strip().lower(),))
        return User.from_row(row) if row else None

    def create_user(self, username: str, password: str, *, role: str = USER,
                    display_name: str = "", must_change: bool = False) -> User:
        username = str(username or "").strip().lower()
        if not USERNAME.match(username):
            raise AuthError(
                "a username is 2-32 characters of lowercase letters, digits, "
                "dot, dash or underscore, starting with a letter or digit")
        if role not in ROLES:
            raise AuthError(f"a role is one of {', '.join(ROLES)}")
        if self.find(username):
            raise AuthError(f"there is already a user called {username!r}", status=409)
        check_password_quality(password, username)

        user = User(
            id="usr-" + uuid.uuid4().hex[:16],
            username=username,
            display_name=(display_name or username).strip()[:80],
            role=role,
            active=True,
            created=_now(),
            must_change=must_change,
        )
        self.db.execute(
            "INSERT INTO users (id, username, display_name, role, active, "
            "password_hash, must_change, created, last_login, failures, "
            "locked_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL)",
            (user.id, user.username, user.display_name, user.role,
             1 if user.active else 0, hash_password(password),
             1 if must_change else 0, user.created),
        )
        return user

    def bootstrap(self, username: str = "admin", password: str | None = None,
                  display_name: str = "") -> tuple[User, str | None]:
        """Make the first administrator, if there is nobody at all yet.

        Returns the user and, when one was generated here, the password to
        show once. A tool nobody can sign in to is not a tool, so an empty
        database gets an account rather than an error - but it is said out
        loud, never silently, and the password is never stored in the clear.
        """
        if self.count():
            raise AuthError("this database already has users")
        chosen = password or os.environ.get("FILLERAI_ADMIN_PASSWORD") or ""
        generated = None
        if not chosen:
            chosen = generated = suggest_password()
        user = self.create_user(username, chosen, role=ADMIN,
                                display_name=display_name or "Administrator",
                                must_change=bool(generated))
        return user, generated

    def set_password(self, user_id: str, password: str,
                     must_change: bool = False) -> User:
        user = self.get(user_id)
        check_password_quality(password, user.username)
        self.db.execute(
            "UPDATE users SET password_hash = ?, must_change = ?, failures = 0, "
            "locked_until = NULL WHERE id = ?",
            (hash_password(password), 1 if must_change else 0, user_id))
        # A changed password ends every other session that account had: that
        # is usually the whole reason it was changed.
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        user.must_change = must_change
        return user

    def set_role(self, user_id: str, role: str) -> User:
        if role not in ROLES:
            raise AuthError(f"a role is one of {', '.join(ROLES)}")
        user = self.get(user_id)
        if user.is_admin and role != ADMIN:
            self._guard_last_admin(user, "demote")
        self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        user.role = role
        return user

    def set_active(self, user_id: str, active: bool) -> User:
        user = self.get(user_id)
        if not active:
            self._guard_last_admin(user, "disable")
        self.db.execute(
            "UPDATE users SET active = ?, failures = 0, locked_until = NULL "
            "WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            # An API token outlives a browser, so turning an account off has
            # to reach them as well or the account is only half off.
            self.db.execute(
                "UPDATE api_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
        user.active = active
        return user

    def set_display_name(self, user_id: str, name: str) -> User:
        user = self.get(user_id)
        display = (str(name or "").strip() or user.username)[:80]
        self.db.execute("UPDATE users SET display_name = ? WHERE id = ?",
                        (display, user_id))
        user.display_name = display
        return user

    def delete_user(self, user_id: str) -> None:
        """Remove a user, their sessions, and everything in their library.

        The library goes with them because it is theirs: leaving a stack of
        unreachable models behind under an id nobody can sign in as is worse
        than either keeping the account or losing the work. Callers are
        expected to have said so out loud first.
        """
        user = self.get(user_id)
        self._guard_last_admin(user, "delete")
        with self.db.transaction() as batch:
            batch.execute("DELETE FROM payloads WHERE owner = ?", (user_id,))
            batch.execute("DELETE FROM entries WHERE owner = ?", (user_id,))
            batch.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            batch.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
            batch.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def _guard_last_admin(self, user: User, verb: str) -> None:
        if user.is_admin and user.active and self.admins() <= 1:
            raise AuthError(
                f"{user.username} is the only administrator left; "
                f"make somebody else an administrator before you {verb} them")

    # -- signing in ---------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User:
        """Check a username and password, or say why not.

        The failure message is the same whether the name is unknown or the
        password is wrong, because the pair of messages people usually write
        here is a way of asking the server which usernames exist.
        """
        row = self.db.one("SELECT * FROM users WHERE username = ?",
                          (str(username or "").strip().lower(),))
        wrong = AuthError("that username and password do not match", status=401)
        if row is None:
            # Spend roughly what a real check spends, so the reply time does
            # not answer the question the message refuses to.
            hash_password(str(password or ""))
            raise wrong

        user = User.from_row(row)
        if not user.active:
            raise AuthError("that account has been turned off", status=403)

        locked = row.get("locked_until")
        if locked and _parse(locked) > dt.datetime.now().astimezone():
            minutes = max(1, int(
                (_parse(locked) - dt.datetime.now().astimezone()).total_seconds() // 60))
            raise AuthError(
                f"too many failed attempts; try again in {minutes} minute(s)",
                status=429)

        if not verify_password(str(row["password_hash"]), str(password or "")):
            self._record_failure(user.id, int(row.get("failures") or 0) + 1)
            raise wrong

        self.db.execute(
            "UPDATE users SET failures = 0, locked_until = NULL, last_login = ? "
            "WHERE id = ?", (_now(), user.id))
        user.last_login = _now()
        return user

    def _record_failure(self, user_id: str, failures: int) -> None:
        locked = None
        if failures >= MAX_FAILURES:
            locked = _stamp(dt.datetime.now().astimezone()
                            + dt.timedelta(minutes=LOCKOUT_MINUTES))
            failures = 0
        self.db.execute("UPDATE users SET failures = ?, locked_until = ? WHERE id = ?",
                        (failures, locked, user_id))

    # -- sessions -----------------------------------------------------------

    def start_session(self, user: User, agent: str = "") -> Session:
        now = dt.datetime.now().astimezone()
        session = Session(
            id=secrets.token_urlsafe(24),
            user_id=user.id,
            csrf=secrets.token_urlsafe(24),
            created=_stamp(now),
            last_seen=_stamp(now),
            expires=_stamp(now + dt.timedelta(hours=IDLE_HOURS)),
        )
        self.db.execute(
            "INSERT INTO sessions (id, user_id, csrf, created, last_seen, "
            "expires, agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session.id, session.user_id, session.csrf, session.created,
             session.last_seen, session.expires, (agent or "")[:200]))
        return session

    def cookie_value(self, session: Session) -> str:
        return f"{session.id}.{self._sign(session.id)}"

    def resolve(self, cookie_value: str) -> tuple[Session, User] | None:
        """The session and user a cookie names, or ``None``.

        Expiry is enforced here rather than by a sweep, so a session that has
        run out is dead the moment it is used, whatever the cleanup did.
        """
        raw = str(cookie_value or "")
        session_id, _, signature = raw.partition(".")
        if not session_id or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(session_id)):
            return None

        row = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        now = dt.datetime.now().astimezone()
        if _parse(str(row["expires"])) <= now or (
                _parse(str(row["created"])) + dt.timedelta(hours=MAX_HOURS) <= now):
            self.end_session(session_id)
            return None

        user_row = self.db.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
        if user_row is None or not bool(user_row["active"]):
            self.end_session(session_id)
            return None

        session = Session(
            id=str(row["id"]), user_id=str(row["user_id"]), csrf=str(row["csrf"]),
            created=str(row["created"]), last_seen=_stamp(now),
            expires=_stamp(now + dt.timedelta(hours=IDLE_HOURS)))
        # Sliding: using the tool keeps you signed in, walking away signs you
        # out. Written on every request, which at one person clicking buttons
        # is a rounding error and at scale would be a batched touch instead.
        self.db.execute("UPDATE sessions SET last_seen = ?, expires = ? WHERE id = ?",
                        (session.last_seen, session.expires, session.id))
        return session, User.from_row(user_row)

    def end_session(self, session_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def end_sessions_for(self, user_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def sweep(self) -> int:
        """Drop sessions that have run out. Called on start; not required."""
        return self.db.execute("DELETE FROM sessions WHERE expires <= ?", (_now(),))

    def sessions_for(self, user_id: str) -> int:
        return self.db.count("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                             (user_id,))

    def _sign(self, value: str) -> str:
        return hmac.new(self._secret, value.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]


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
