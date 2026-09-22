"""API tokens: the credential an application authenticates with.

These cover the rules that make a bearer credential safe to hand out - that
the secret is never stored, that a failure says nothing about why, and that
turning an account off turns its tokens off with it - rather than the SQL
underneath them.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.auth import ADMIN, Auth
from fillerai.db import connect
from fillerai.tokens import MAX_TOKENS_PER_USER, ApiToken, TokenError, Tokens


class TokenCase(unittest.TestCase):
    def setUp(self):
        self.db = connect("sqlite://:memory:")
        self.auth = Auth(self.db)
        self.user = self.auth.create_user("krishna", "a-long-enough-password")
        self.other = self.auth.create_user("someone", "another-long-password")
        self.tokens = Tokens(self.db)

    def tearDown(self):
        self.db.close()


class TestIssuing(TokenCase):
    def test_a_token_verifies_and_names_its_owner(self):
        record, secret = self.tokens.issue(self.user.id, "claims desk")
        found = self.tokens.verify(secret)
        self.assertEqual(found.id, record.id)
        self.assertEqual(found.user_id, self.user.id)
        self.assertEqual(found.name, "claims desk")

    def test_the_secret_is_not_stored_anywhere(self):
        _record, secret = self.tokens.issue(self.user.id)
        tail = secret.split("_")[-1]
        rows = self.db.query("SELECT * FROM api_tokens")
        self.assertEqual(len(rows), 1)
        for value in rows[0].values():
            self.assertNotIn(tail, str(value))

    def test_a_secret_with_underscores_in_it_still_verifies(self):
        # secrets.token_urlsafe emits '_' and '-', so the only structure in a
        # token is its first two underscores. Roughly one token in three has
        # another, which made this a flake before it was a test.
        for _ in range(60):
            record, secret = self.tokens.issue(self.user.id)
            if "_" in secret.split("_", 2)[2]:
                self.assertEqual(self.tokens.verify(secret).id, record.id)
                return
        self.skipTest("no secret with an underscore came up in 60 tries")

    def test_two_tokens_are_never_the_same(self):
        first = self.tokens.issue(self.user.id)[1]
        second = self.tokens.issue(self.user.id)[1]
        self.assertNotEqual(first, second)

    def test_a_token_says_what_it_is_without_being_one(self):
        record, secret = self.tokens.issue(self.user.id, "staging")
        shown = record.to_dict()
        self.assertTrue(secret.startswith(shown["prefix"]))
        self.assertNotIn(secret, str(shown))
        self.assertNotIn("secret_hash", shown)

    def test_a_nameless_token_still_has_something_to_call_it(self):
        record, _ = self.tokens.issue(self.user.id, "   ")
        self.assertTrue(record.name)

    def test_there_is_a_ceiling_on_how_many_one_account_can_hold(self):
        for index in range(MAX_TOKENS_PER_USER):
            self.tokens.issue(self.user.id, f"app {index}")
        with self.assertRaises(TokenError):
            self.tokens.issue(self.user.id, "one too many")
        # Revoking one makes room again, which is the point of the ceiling.
        self.tokens.revoke(self.tokens.list(self.user.id)[0].id)
        self.tokens.issue(self.user.id, "room now")


class TestRefusing(TokenCase):
    def test_every_failure_reads_the_same(self):
        _record, secret = self.tokens.issue(self.user.id)
        # Only the first two underscores are structure: a url-safe secret can
        # contain them too, which is why nothing here splits without a limit.
        head, token_id, tail = secret.split("_", 2)
        wrong = [
            "",
            "not-a-token",
            "Bearer flr_aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbb",
            f"{head}_{token_id}_wrongwrongwrongwrong",
            f"{head}_aaaaaaaaaaaa_{tail}",
            secret + "x",
        ]
        messages = set()
        for candidate in wrong:
            with self.subTest(candidate[:24]):
                with self.assertRaises(TokenError) as caught:
                    self.tokens.verify(candidate)
                self.assertEqual(caught.exception.status, 401)
                messages.add(caught.exception.message)
        self.assertEqual(len(messages), 1, messages)

    def test_a_revoked_token_stops_working_and_stays_on_the_record(self):
        record, secret = self.tokens.issue(self.user.id, "gone")
        self.tokens.revoke(record.id)
        with self.assertRaises(TokenError):
            self.tokens.verify(secret)
        self.assertEqual(self.tokens.list(self.user.id), [])
        kept = self.tokens.list(self.user.id, include_revoked=True)
        self.assertEqual([t.name for t in kept], ["gone"])

    def test_an_expired_token_is_refused_and_says_so(self):
        record, secret = self.tokens.issue(self.user.id, "short", days=1)
        self.assertFalse(record.expired())
        # Move its expiry into the past rather than waiting a day.
        past = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat()
        self.db.execute("UPDATE api_tokens SET expires = ? WHERE id = ?",
                        (past, record.id))
        with self.assertRaises(TokenError) as caught:
            self.tokens.verify(secret)
        self.assertIn("expired", caught.exception.message)

    def test_an_expiry_in_the_past_is_refused_at_issue(self):
        with self.assertRaises(TokenError):
            self.tokens.issue(self.user.id, days=0)


class TestFollowingTheAccount(TokenCase):
    def test_disabling_an_account_revokes_its_tokens(self):
        _record, secret = self.tokens.issue(self.user.id, "desk")
        self.auth.create_user("boss", "a-long-enough-password", role=ADMIN)
        self.auth.set_active(self.user.id, False)
        with self.assertRaises(TokenError):
            self.tokens.verify(secret)

    def test_deleting_an_account_takes_its_tokens_with_it(self):
        self.tokens.issue(self.user.id, "desk")
        self.auth.create_user("boss", "a-long-enough-password", role=ADMIN)
        self.auth.delete_user(self.user.id)
        self.assertEqual(self.db.count("SELECT COUNT(*) FROM api_tokens"), 0)

    def test_one_account_never_sees_another_account_s_tokens(self):
        self.tokens.issue(self.user.id, "mine")
        self.tokens.issue(self.other.id, "theirs")
        self.assertEqual([t.name for t in self.tokens.list(self.user.id)], ["mine"])
        self.assertEqual([t.name for t in self.tokens.list(self.other.id)], ["theirs"])


class TestUsing(TokenCase):
    def test_last_used_starts_empty_and_is_stamped_on_request(self):
        record, _secret = self.tokens.issue(self.user.id)
        self.assertIsNone(record.last_used)
        self.tokens.touch(record.id)
        self.assertTrue(self.tokens.get(record.id).last_used)

    def test_a_token_can_be_pinned_to_one_model(self):
        record, _secret = self.tokens.issue(self.user.id, model_id="mdl-1")
        self.assertEqual(self.tokens.get(record.id).model_id, "mdl-1")


class TestTheRecord(unittest.TestCase):
    def test_an_unexpiring_token_never_expires(self):
        self.assertFalse(ApiToken("a" * 12, "usr-1", "x", "now").expired())

    def test_an_unreadable_expiry_is_treated_as_long_past(self):
        # Rather than crashing a request on a row somebody edited by hand.
        self.assertTrue(
            ApiToken("a" * 12, "usr-1", "x", "now", expires="not a date").expired())


if __name__ == "__main__":
    unittest.main()
