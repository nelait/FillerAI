"""Tests for accounts and sessions.

Weighted towards the refusals rather than the happy path, because the happy
path is one call and the refusals are the whole point: a wrong password, a
disabled account, a cookie somebody edited, and the click that would leave
the tool with no administrator.
"""

from __future__ import annotations

import datetime as dt
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.auth import (
    ADMIN,
    USER,
    Auth,
    AuthError,
    check_password_quality,
    hash_password,
    suggest_password,
    verify_password,
)
from fillerai.db import connect
from fillerai.dbstore import DatabaseStore


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.db = connect("sqlite://:memory:")
        self.addCleanup(self.db.dispose)
        self.auth = Auth(self.db)
        self.admin = self.auth.create_user("root", "a-good-password", role=ADMIN)


class TestPasswords(unittest.TestCase):
    def test_a_hash_verifies_and_a_wrong_password_does_not(self):
        stored = hash_password("correct horse battery")
        self.assertTrue(verify_password(stored, "correct horse battery"))
        self.assertFalse(verify_password(stored, "correct horse batter"))

    def test_the_same_password_hashes_differently_every_time(self):
        self.assertNotEqual(hash_password("same one"), hash_password("same one"))

    def test_a_pbkdf2_hash_still_verifies(self):
        """The fallback format, so a hash written by another build still works."""
        import base64
        import hashlib

        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"whatever", salt, 1000)
        stored = "$".join(("pbkdf2_sha256", "1000",
                           base64.b64encode(salt).decode(),
                           base64.b64encode(digest).decode()))
        self.assertTrue(verify_password(stored, "whatever"))
        self.assertFalse(verify_password(stored, "something else"))

    def test_a_hash_that_is_nonsense_is_a_failed_login_not_a_crash(self):
        for nonsense in ("", "not-a-hash", "scrypt$broken", "$$$$"):
            with self.subTest(nonsense):
                self.assertFalse(verify_password(nonsense, "anything"))

    def test_the_rules_are_length_and_not_your_own_name(self):
        check_password_quality("long enough to pass")
        with self.assertRaises(AuthError):
            check_password_quality("short")
        with self.assertRaises(AuthError):
            check_password_quality("Krishna", "krishna")
        with self.assertRaises(AuthError):
            check_password_quality("x" * 2000)

    def test_a_generated_password_is_long_and_never_the_same_twice(self):
        first, second = suggest_password(), suggest_password()
        self.assertNotEqual(first, second)
        check_password_quality(first)


class TestAccounts(AuthCase):
    def test_a_username_is_normalised_and_checked(self):
        user = self.auth.create_user("Krishna", "a-good-password")
        self.assertEqual(user.username, "krishna")
        for bad in ("", "a", "has space", "UPPER!", "x" * 40, "-leading"):
            with self.subTest(bad), self.assertRaises(AuthError):
                self.auth.create_user(bad, "a-good-password")

    def test_two_people_cannot_share_a_name(self):
        self.auth.create_user("krishna", "a-good-password")
        with self.assertRaises(AuthError) as caught:
            self.auth.create_user("krishna", "another-password")
        self.assertEqual(caught.exception.status, 409)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(AuthError):
            self.auth.create_user("someone", "a-good-password", role="superuser")

    def test_the_hash_never_leaves_the_module(self):
        shown = self.admin.to_dict()
        self.assertNotIn("password_hash", shown)
        self.assertNotIn("failures", shown)

    def test_bootstrap_makes_the_first_administrator_and_only_the_first(self):
        db = connect("sqlite://:memory:")
        self.addCleanup(db.dispose)
        auth = Auth(db)
        user, generated = auth.bootstrap()
        self.assertTrue(user.is_admin)
        self.assertTrue(user.must_change)
        self.assertTrue(generated)
        self.assertEqual(auth.authenticate("admin", generated).id, user.id)
        with self.assertRaises(AuthError):
            auth.bootstrap()


class TestSigningIn(AuthCase):
    def test_the_right_password_works_and_records_the_time(self):
        user = self.auth.authenticate("root", "a-good-password")
        self.assertEqual(user.id, self.admin.id)
        self.assertTrue(self.auth.get(user.id).last_login)

    def test_an_unknown_name_and_a_wrong_password_read_the_same(self):
        with self.assertRaises(AuthError) as missing:
            self.auth.authenticate("nobody", "a-good-password")
        with self.assertRaises(AuthError) as wrong:
            self.auth.authenticate("root", "not-the-password")
        self.assertEqual(missing.exception.message, wrong.exception.message)
        self.assertEqual(missing.exception.status, 401)

    def test_a_disabled_account_is_told_why(self):
        other = self.auth.create_user("krishna", "a-good-password")
        self.auth.set_active(other.id, False)
        with self.assertRaises(AuthError) as caught:
            self.auth.authenticate("krishna", "a-good-password")
        self.assertEqual(caught.exception.status, 403)

    def test_guessing_repeatedly_earns_a_pause(self):
        for _ in range(6):
            with self.assertRaises(AuthError):
                self.auth.authenticate("root", "wrong")
        with self.assertRaises(AuthError) as caught:
            self.auth.authenticate("root", "a-good-password")
        self.assertEqual(caught.exception.status, 429)

    def test_signing_in_clears_the_count(self):
        for _ in range(3):
            with self.assertRaises(AuthError):
                self.auth.authenticate("root", "wrong")
        self.auth.authenticate("root", "a-good-password")
        row = self.db.one("SELECT failures FROM users WHERE id = ?", (self.admin.id,))
        self.assertEqual(row["failures"], 0)


class TestSessions(AuthCase):
    def cookie(self):
        session = self.auth.start_session(self.admin)
        return session, self.auth.cookie_value(session)

    def test_a_cookie_resolves_to_its_user(self):
        _session, cookie = self.cookie()
        found = self.auth.resolve(cookie)
        self.assertIsNotNone(found)
        self.assertEqual(found[1].username, "root")

    def test_an_edited_cookie_resolves_to_nobody(self):
        session, cookie = self.cookie()
        for tampered in ("", "nonsense", session.id, f"{session.id}.beef",
                         cookie[:-1] + ("0" if cookie[-1] != "0" else "1")):
            with self.subTest(tampered):
                self.assertIsNone(self.auth.resolve(tampered))

    def test_a_cookie_signed_with_another_databases_key_is_refused(self):
        other_db = connect("sqlite://:memory:")
        self.addCleanup(other_db.dispose)
        other = Auth(other_db)
        stranger = other.create_user("root", "a-good-password")
        cookie = other.cookie_value(other.start_session(stranger))
        self.assertIsNone(self.auth.resolve(cookie))

    def test_a_session_that_has_run_out_is_dead_when_it_is_used(self):
        session, cookie = self.cookie()
        past = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat()
        self.db.execute("UPDATE sessions SET expires = ? WHERE id = ?",
                        (past, session.id))
        self.assertIsNone(self.auth.resolve(cookie))
        self.assertEqual(self.auth.sessions_for(self.admin.id), 0)

    def test_using_a_session_pushes_its_expiry_out(self):
        session, cookie = self.cookie()
        soon = (dt.datetime.now().astimezone() + dt.timedelta(minutes=2)).isoformat()
        self.db.execute("UPDATE sessions SET expires = ? WHERE id = ?",
                        (soon, session.id))
        self.auth.resolve(cookie)
        row = self.db.one("SELECT expires FROM sessions WHERE id = ?", (session.id,))
        self.assertGreater(row["expires"], soon)

    def test_signing_out_ends_it_everywhere(self):
        session, cookie = self.cookie()
        self.auth.end_session(session.id)
        self.assertIsNone(self.auth.resolve(cookie))

    def test_disabling_an_account_ends_its_sessions(self):
        other = self.auth.create_user("krishna", "a-good-password")
        cookie = self.auth.cookie_value(self.auth.start_session(other))
        self.auth.set_active(other.id, False)
        self.assertIsNone(self.auth.resolve(cookie))

    def test_changing_a_password_ends_every_session(self):
        _session, cookie = self.cookie()
        self.auth.set_password(self.admin.id, "a-different-password")
        self.assertIsNone(self.auth.resolve(cookie))

    def test_sweeping_drops_only_what_has_run_out(self):
        live, _ = self.cookie()
        dead = self.auth.start_session(self.admin)
        past = (dt.datetime.now().astimezone() - dt.timedelta(hours=1)).isoformat()
        self.db.execute("UPDATE sessions SET expires = ? WHERE id = ?",
                        (past, dead.id))
        self.auth.sweep()
        self.assertEqual(self.auth.sessions_for(self.admin.id), 1)
        self.assertTrue(self.db.one("SELECT id FROM sessions WHERE id = ?",
                                    (live.id,)))


class TestTheLastAdministrator(AuthCase):
    def test_cannot_be_disabled_demoted_or_deleted(self):
        for act in (lambda: self.auth.set_active(self.admin.id, False),
                    lambda: self.auth.set_role(self.admin.id, USER),
                    lambda: self.auth.delete_user(self.admin.id)):
            with self.subTest(act), self.assertRaises(AuthError) as caught:
                act()
            self.assertIn("only administrator", caught.exception.message)

    def test_can_be_once_there_is_another_one(self):
        second = self.auth.create_user("second", "a-good-password", role=ADMIN)
        self.auth.set_role(self.admin.id, USER)
        self.assertEqual(self.auth.get(self.admin.id).role, USER)
        self.assertTrue(self.auth.get(second.id).is_admin)


class TestDeleting(AuthCase):
    def test_a_deleted_user_takes_their_library_with_them(self):
        other = self.auth.create_user("krishna", "a-good-password")
        mine = DatabaseStore(self.db, other.id)
        theirs = DatabaseStore(self.db, self.admin.id)
        mine.save_source("<form></form>")
        kept = theirs.save_source("<form></form>")
        self.auth.start_session(other)

        self.auth.delete_user(other.id)

        self.assertEqual(mine.list(), [])
        self.assertEqual(self.auth.sessions_for(other.id), 0)
        self.assertEqual(self.db.count("SELECT COUNT(*) FROM payloads"), 1)
        self.assertTrue(theirs.has(kept.id))
        with self.assertRaises(AuthError):
            self.auth.get(other.id)


class TestTheCommands(unittest.TestCase):
    """``fillerai users`` and ``fillerai db``.

    They exist for the case the admin panel cannot help with - nobody can
    sign in - so they are tested the way they would be used: against a
    database file, from nothing.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="fillerai-cli-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.url = f"sqlite://{Path(self.folder) / 'fillerai.db'}"

    def run_cli(self, *argv):
        from fillerai.cli import main

        with redirect_stdout(io.StringIO()) as out:
            code = main([argv[0], "--database", self.url, *argv[1:]])
        return code, out.getvalue()

    def opened(self) -> Auth:
        return Auth(connect(self.url))

    def test_the_first_administrator_can_be_made_from_a_shell(self):
        code, printed = self.run_cli("users", "add", "root", "--admin")
        self.assertEqual(code, 0)
        self.assertIn("created root (admin)", printed)
        password = printed.split("password:")[1].split()[0]
        self.assertEqual(self.opened().authenticate("root", password).role, ADMIN)

    def test_a_chosen_password_is_not_printed_back(self):
        _code, printed = self.run_cli("users", "add", "krishna",
                                      "--password", "a-password-they-chose")
        self.assertNotIn("a-password-they-chose", printed)
        self.assertEqual(self.opened().authenticate(
            "krishna", "a-password-they-chose").username, "krishna")

    def test_listing_says_who_is_there_and_who_is_off(self):
        self.run_cli("users", "add", "root", "--admin")
        self.run_cli("users", "add", "krishna")
        self.run_cli("users", "disable", "krishna")
        _code, printed = self.run_cli("users", "list")
        self.assertIn("root", printed)
        self.assertIn("(disabled)", printed)

    def test_a_password_can_be_reset_from_a_shell(self):
        self.run_cli("users", "add", "krishna", "--password", "the-old-password")
        _code, printed = self.run_cli("users", "passwd", "krishna")
        new = printed.split("password:")[1].split()[0]
        auth = self.opened()
        self.assertTrue(auth.authenticate("krishna", new))
        with self.assertRaises(AuthError):
            auth.authenticate("krishna", "the-old-password")

    def test_a_role_can_be_changed_and_the_last_admin_protected(self):
        self.run_cli("users", "add", "root", "--admin")
        code, _printed = self.run_cli("users", "role", "root", "user")
        self.assertEqual(code, 1)
        self.run_cli("users", "add", "second", "--admin")
        self.assertEqual(self.run_cli("users", "role", "root", "user")[0], 0)
        self.assertEqual(self.opened().find("root").role, USER)

    def test_an_unknown_name_is_an_error_not_a_traceback(self):
        for argv in (("users", "passwd", "ghost"), ("users", "role", "ghost", "user"),
                     ("users", "disable", "ghost"), ("users", "delete", "ghost")):
            with self.subTest(argv):
                self.assertEqual(self.run_cli(*argv)[0], 1)

    def test_deleting_somebody_with_a_library_asks_first(self):
        self.run_cli("users", "add", "krishna", "--password", "a-good-password")
        auth = self.opened()
        DatabaseStore(auth.db, auth.find("krishna").id).save_source("<form></form>")

        self.assertEqual(self.run_cli("users", "delete", "krishna")[0], 1)
        self.assertIsNotNone(self.opened().find("krishna"))
        self.assertEqual(self.run_cli("users", "delete", "krishna", "--yes")[0], 0)
        self.assertIsNone(self.opened().find("krishna"))

    def test_status_says_what_is_in_there(self):
        self.run_cli("users", "add", "root", "--admin")
        code, printed = self.run_cli("db", "status")
        self.assertEqual(code, 0)
        self.assertIn("SQLiteDatabase", printed)
        self.assertIn("1 user(s)", printed)

    def test_a_file_library_can_be_imported_for_somebody(self):
        from fillerai.store import Store

        self.run_cli("users", "add", "krishna", "--password", "a-good-password")
        files = Store(tempfile.mkdtemp(prefix="fillerai-cli-files-"))
        self.addCleanup(shutil.rmtree, files.root, ignore_errors=True)
        source = files.save_source("<form></form>", name="claims")
        files.put("schema", "claims", {"version": 1}, parent=source.id)

        code, printed = self.run_cli("db", "import", "--from", str(files.root),
                                     "--user", "krishna")
        self.assertEqual(code, 0)
        self.assertIn("copied 2", printed)

        auth = self.opened()
        theirs = DatabaseStore(auth.db, auth.find("krishna").id)
        self.assertEqual(len(theirs.list()), 2)
        self.assertTrue(theirs.has(source.id))

        # Again, and nothing is duplicated.
        _code, printed = self.run_cli("db", "import", "--from", str(files.root),
                                      "--user", "krishna")
        self.assertIn("copied 0", printed)

    def test_importing_for_somebody_who_is_not_there_is_refused(self):
        code, _printed = self.run_cli("db", "import", "--user", "ghost")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
