"""API keys somebody typed into the UI, held in memory and nowhere else.

The rest of this project takes a hard line about the key: read from the
environment, never written to the library, never into the SQLite store, never
logged, never echoed back by any command. Offering a box to paste one into is
a request to soften that, and the softening has to stop somewhere sensible.

**It stops at the disk.** A key typed into the UI lives in this process, in a
dictionary, for as long as the server runs. It is not written to the database,
not to the file library, not to a config file, and not to the log. Restarting
the server forgets every one of them, and the UI says so where the box is,
because a promise the user cannot see is not a promise.

That is a real cost - somebody who restarts often will retype - and the
alternative is worse. A plaintext bearer credential for a third-party account,
sitting in a SQLite file that the admin page will happily export and that gets
copied around with the library, is the kind of thing nobody regrets until they
do. Anybody who wants the key to survive a restart has the environment
variable, which is where a secret belongs and which this does not replace.

**Each user's keys are their own.** Accounts already give each person their
own library; a shared key would quietly bill one person for another's runs.
The store is keyed by user id, and by the empty string when the server is
running without accounts, which is the single-user tool.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field as dc_field
from typing import Any

#: The key of the row used when there are no accounts.
SINGLE_USER = ""


@dataclass
class Preference:
    """What one person chose in the UI, as opposed to what they exported."""

    #: ``anthropic``, ``openai``, or "" for "work it out from my keys".
    provider: str = ""
    #: Overrides the per-task default for every task. "" for neither.
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model}


@dataclass
class _Row:
    keys: dict[str, str] = dc_field(default_factory=dict)
    preference: Preference = dc_field(default_factory=Preference)


class Keyring:
    """Per-user API keys and model preferences, for this run of the server.

    Every method takes the user id the request arrived as. Nothing here
    validates a key - a wrong one comes back as a 401 from the service, with
    the provider's own wording, which is more use than a guess made here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, _Row] = {}

    # -- keys --------------------------------------------------------------

    def set_key(self, user: str, provider: str, key: str) -> None:
        """Remember a key for one provider, or forget it when ``key`` is empty."""
        key = key.strip()
        with self._lock:
            row = self._rows.setdefault(user, _Row())
            if key:
                row.keys[provider] = key
            else:
                row.keys.pop(provider, None)

    def keys(self, user: str) -> dict[str, str]:
        """Every key this user has typed, by provider. A copy."""
        with self._lock:
            row = self._rows.get(user)
            return dict(row.keys) if row else {}

    def typed(self, user: str) -> list[str]:
        """Which providers this user has a typed key for. Never the keys."""
        return sorted(self.keys(user))

    # -- preferences -------------------------------------------------------

    def preference(self, user: str) -> Preference:
        with self._lock:
            row = self._rows.get(user)
            return Preference(**row.preference.to_dict()) if row else Preference()

    def set_preference(self, user: str, *, provider: str | None = None,
                       model: str | None = None) -> Preference:
        with self._lock:
            row = self._rows.setdefault(user, _Row())
            if provider is not None:
                row.preference.provider = provider.strip()
            if model is not None:
                row.preference.model = model.strip()
            return Preference(**row.preference.to_dict())

    # -- housekeeping ------------------------------------------------------

    def forget(self, user: str) -> None:
        """Drop everything this user typed. Called when their account goes."""
        with self._lock:
            self._rows.pop(user, None)

    def clear(self) -> None:
        """Drop everything. For tests, and for closing a database."""
        with self._lock:
            self._rows.clear()


__all__ = ["Keyring", "Preference", "SINGLE_USER"]
