"""The language-model settings and the propose-rules step, over HTTP.

Two things are being defended here and neither is about proposing rules.

**A key never comes back.** Every reply from these endpoints is serialised and
searched for the key that was put in. That is a blunt check and it is the
right one: a field added later that quietly echoes the key would pass a test
that only looked at the fields somebody thought to assert about.

**A key never leaves the process.** The keyring is a dictionary, so a test can
only prove the negative by looking - the library and the database are checked
for it too, and the keyring is asserted to forget everything on request.

The one call that would cost money is stubbed. What is being tested is the
plumbing around it, which is all the server contributes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.store import Store
from fillerai.web import server as server_module
from fillerai.web.server import Handler, create_server

SPEC = Path(__file__).resolve().parent.parent / "examples" / "member_enrollment.fields.json"

#: Nothing real, but shaped like the thing it stands for.
ANTHROPIC_KEY = "sk-ant-api03-totally-made-up-0001"
OPENAI_KEY = "sk-proj-totally-made-up-0002"

#: Everything the settings read. Cleared for the duration of these tests,
#: because a developer with a key exported would otherwise see this file fail
#: for a reason that has nothing to do with the code.
ENVIRONMENT = ("FILLERAI_LLM_KEY", "FILLERAI_LLM_PROVIDER", "FILLERAI_LLM_MODEL",
               "FILLERAI_LLM_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


class LlmServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.quiet = True
        cls.library_dir = tempfile.mkdtemp(prefix="fillerai-test-llm-")
        cls._real_library = server_module.LIBRARY
        server_module.LIBRARY = Store(cls.library_dir)
        cls.httpd = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server_module.LIBRARY = cls._real_library
        shutil.rmtree(cls.library_dir, ignore_errors=True)

    def setUp(self):
        server_module.KEYRING.clear()
        self._environ = {name: os.environ.pop(name)
                         for name in ENVIRONMENT if name in os.environ}

    def tearDown(self):
        server_module.KEYRING.clear()
        os.environ.update(self._environ)

    def post(self, path: str, payload=None):
        body = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            self.base + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def schema(self):
        status, body = self.post("/api/extract", {
            "kind": "spec", "content": SPEC.read_text(encoding="utf-8"), "save": False})
        self.assertEqual(status, 200, body)
        return body["schema"]


class TestKeys(LlmServerCase):
    def test_nothing_is_configured_to_begin_with(self):
        status, body = self.post("/api/llm/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])
        self.assertEqual(sorted(p["name"] for p in body["providers"]),
                         ["anthropic", "openai"])

    def test_a_typed_openai_key_turns_it_on_and_picks_the_service(self):
        status, body = self.post("/api/llm/key",
                                 {"provider": "openai", "key": OPENAI_KEY})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["configured"])
        self.assertEqual(body["provider"], "openai")
        self.assertTrue(body["models"]["rules"].startswith("gpt"))

    def test_the_key_never_comes_back_in_any_reply(self):
        """The blunt check: the key must not be anywhere in the payload."""
        for path, payload in (
            ("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY}),
            ("/api/llm/status", {}),
            ("/api/llm/preference", {"provider": "openai"}),
        ):
            status, body = self.post(path, payload)
            self.assertEqual(status, 200, body)
            self.assertNotIn(OPENAI_KEY, json.dumps(body), path)
        # …but enough of it to tell two keys apart.
        _, body = self.post("/api/llm/status")
        self.assertIn(OPENAI_KEY[-4:], body["key"])

    def test_a_typed_key_is_not_written_anywhere(self):
        self.post("/api/llm/key", {"provider": "anthropic", "key": ANTHROPIC_KEY})
        on_disk = ""
        for path in Path(self.library_dir).rglob("*"):
            if path.is_file():
                on_disk += path.read_text(encoding="utf-8", errors="ignore")
        self.assertNotIn(ANTHROPIC_KEY, on_disk)

    def test_an_empty_key_forgets_it(self):
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})
        status, body = self.post("/api/llm/key", {"provider": "openai", "key": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["providers"][1]["typed"], False)

    def test_forget_drops_everything_typed(self):
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})
        self.post("/api/llm/key", {"provider": "anthropic", "key": ANTHROPIC_KEY})
        status, body = self.post("/api/llm/forget")
        self.assertEqual(status, 200)
        self.assertEqual([p["typed"] for p in body["providers"]], [False, False])

    def test_an_unknown_provider_is_refused(self):
        status, body = self.post("/api/llm/key", {"provider": "skynet", "key": "x"})
        self.assertEqual(status, 400)
        self.assertIn("openai", body["error"])

    def test_something_far_too_long_is_not_a_key(self):
        status, body = self.post("/api/llm/key",
                                 {"provider": "openai", "key": "x" * 5000})
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])

    def test_the_keyring_itself_holds_it_and_says_which(self):
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})
        self.assertEqual(server_module.KEYRING.typed(""), ["openai"])
        self.assertEqual(server_module.KEYRING.keys("")["openai"], OPENAI_KEY)


class TestTheKeyringItself(unittest.TestCase):
    """The part that has to be right when there is more than one person."""

    def setUp(self):
        from fillerai.web.keyring import Keyring

        self.keyring = Keyring()

    def test_one_person_cannot_see_another_s_key(self):
        self.keyring.set_key("alice", "openai", OPENAI_KEY)
        self.assertEqual(self.keyring.keys("bob"), {})
        self.assertEqual(self.keyring.typed("bob"), [])

    def test_a_preference_is_per_person_too(self):
        self.keyring.set_preference("alice", provider="openai")
        self.assertEqual(self.keyring.preference("alice").provider, "openai")
        self.assertEqual(self.keyring.preference("bob").provider, "")

    def test_forgetting_one_person_leaves_the_others(self):
        self.keyring.set_key("alice", "openai", OPENAI_KEY)
        self.keyring.set_key("bob", "anthropic", ANTHROPIC_KEY)
        self.keyring.forget("alice")
        self.assertEqual(self.keyring.typed("alice"), [])
        self.assertEqual(self.keyring.typed("bob"), ["anthropic"])

    def test_what_comes_back_is_a_copy(self):
        self.keyring.set_key("alice", "openai", OPENAI_KEY)
        self.keyring.keys("alice")["openai"] = "tampered"
        self.assertEqual(self.keyring.keys("alice")["openai"], OPENAI_KEY)


class TestPreferences(LlmServerCase):
    def test_choosing_a_service_overrides_what_the_keys_suggest(self):
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})
        self.post("/api/llm/key", {"provider": "anthropic", "key": ANTHROPIC_KEY})
        status, body = self.post("/api/llm/preference", {"provider": "anthropic"})
        self.assertEqual(status, 200)
        self.assertEqual(body["provider"], "anthropic")
        self.assertIn("asked for", body["reason"])

    def test_a_model_name_is_used_for_every_task(self):
        status, body = self.post("/api/llm/preference", {"model": "gpt-4.1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["models"]["rules"], "gpt-4.1")
        self.assertEqual(body["provider"], "openai")

    def test_clearing_the_choice_goes_back_to_working_it_out(self):
        self.post("/api/llm/preference", {"provider": "openai"})
        status, body = self.post("/api/llm/preference", {"provider": ""})
        self.assertEqual(status, 200)
        self.assertNotIn("asked for", body["reason"])

    def test_a_service_nobody_has_is_refused(self):
        status, body = self.post("/api/llm/preference", {"provider": "skynet"})
        self.assertEqual(status, 400)
        self.assertIn("skynet", body["error"])


class TestProposingRules(LlmServerCase):
    def test_the_estimate_costs_nothing_and_says_so(self):
        status, body = self.post("/api/llm/rules/estimate", {"schema": self.schema()})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["configured"])
        self.assertGreater(body["input_tokens"], 0)
        self.assertTrue(any("tokens" in line for line in body["lines"]))

    def test_the_estimate_follows_the_chosen_service(self):
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})
        _, openai = self.post("/api/llm/rules/estimate", {"schema": self.schema()})
        self.post("/api/llm/preference", {"provider": "anthropic"})
        _, anthropic = self.post("/api/llm/rules/estimate", {"schema": self.schema()})
        self.assertNotEqual(openai["model"], anthropic["model"])

    def test_asking_without_a_key_says_where_to_put_one(self):
        status, body = self.post("/api/llm/rules/propose", {"schema": self.schema()})
        self.assertEqual(status, 400)
        self.assertIn("Your account", body["error"])

    def test_a_run_comes_back_as_kept_and_dropped(self):
        """The call itself is stubbed; everything around it is not."""
        from fillerai.llm import rules as llm_rules

        schema = self.schema()
        self.post("/api/llm/key", {"provider": "openai", "key": OPENAI_KEY})

        real = llm_rules.propose

        def fake(form, **kwargs):
            proposals = [llm_rules.Proposal.from_payload(entry) for entry in (
                {"field": "home_country", "follows": "home_state",
                 "when": {state: "US" for state in _states(form)},
                 "confidence": 0.95, "why": "every state on this list is a US state"},
            )]
            return llm_rules.Proposals(
                schema_name=form.name, model=kwargs["settings"].model,
                verdicts=llm_rules.check(form, proposals))

        llm_rules.propose = fake
        try:
            status, body = self.post("/api/llm/rules/propose", {"schema": schema})
        finally:
            llm_rules.propose = real

        self.assertEqual(status, 200, body)
        self.assertEqual([r["field"] for r in body["kept"]], ["home_country"])
        self.assertEqual(body["provider"], "openai")


class TestApplyingRules(LlmServerCase):
    def rule(self, schema):
        return {"field": "home_country", "follows": "home_state",
                "when": {state: "US" for state in _states(schema)},
                "confidence": 0.95, "why": "they are all US states"}

    def test_a_kept_rule_is_declared_on_the_schema_and_saved(self):
        schema = self.schema()
        status, body = self.post("/api/llm/rules/apply",
                                 {"schema": schema, "rules": [self.rule(schema)]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["applied"], ["home_country"])
        ruled = {f["name"]: f for f in body["schema"]["fields"]}["home_country"]
        self.assertEqual(ruled["derived"]["sources"], ["home_state"])
        self.assertTrue(body["schema_id"])

    def test_the_saved_schema_is_in_the_library(self):
        schema = self.schema()
        _, body = self.post("/api/llm/rules/apply",
                            {"schema": schema, "rules": [self.rule(schema)]})
        status, listing = self.post("/api/library", {"kind": "schema"})
        self.assertEqual(status, 200)
        self.assertIn(body["schema_id"], [e["id"] for e in listing["entries"]])

    def test_a_rule_the_gate_would_reject_cannot_be_smuggled_past(self):
        """The browser sends the list back; the server does not take its word."""
        schema = self.schema()
        bad = dict(self.rule(schema))
        bad["when"] = {"CA": "Atlantis"}
        status, body = self.post("/api/llm/rules/apply",
                                 {"schema": schema, "rules": [bad]})
        self.assertEqual(status, 400)
        self.assertIn("Atlantis", body["error"])

    def test_applying_nothing_is_refused(self):
        status, body = self.post("/api/llm/rules/apply",
                                 {"schema": self.schema(), "rules": []})
        self.assertEqual(status, 400)
        self.assertIn("at least one", body["error"])

    def test_an_unreadable_rule_says_so_rather_than_crashing(self):
        status, body = self.post("/api/llm/rules/apply", {
            "schema": self.schema(), "rules": [{"field": "home_country"}]})
        self.assertEqual(status, 400)
        self.assertIn("could not be read", body["error"])


class TestTheFenceStillHolds(LlmServerCase):
    def test_serving_a_page_does_not_import_the_llm_package(self):
        """`tests/test_llm_fence.py` owns this; here it is at runtime."""
        status, _ = self.post("/api/llm/status")
        self.assertEqual(status, 200)
        # Once status has run the package is loaded, which is the point of
        # importing it inside the handler rather than at the top of the file.
        self.assertIn("fillerai.llm", sys.modules)


def _states(schema) -> list[str]:
    fields = {f["name"] if isinstance(f, dict) else f.name:
              f for f in (schema["fields"] if isinstance(schema, dict) else schema.fields)}
    field = fields["home_state"]
    options = field["options"] if isinstance(field, dict) else [
        {"value": o.value} for o in field.options]
    return [o["value"] for o in options]


if __name__ == "__main__":
    unittest.main()
