"""Bot Builder: templates, reading a phrase, and the turn.

The turn is a pure function of the input and the state, so almost all of this
is one call and an assertion - which is the reason it was built that way. The
contract being tested is ``docs/bot-builder.md``; where a test pins a promise
made there, the docstring says which.
"""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.bot import BotError, model_completer, take_turn
from fillerai.bot import template as templates
from fillerai.bot import understand
from fillerai.bot.template import Template, TemplateError, from_schema
from fillerai.schema import Constraints, Field, FormSchema, Option
from fillerai.store import Store

STARTERS = templates.starters()
ADDRESS = STARTERS["address_change"]
DOCUMENT = STARTERS["document_request"]
BOTH = [ADDRESS, DOCUMENT]

ON_FILE = {"street_address": "55 Market St", "unit": "", "city": "San Francisco",
           "state": "CA", "postal_code": "94105", "country": "US",
           "policy_number": "PA-1048822", "full_name": "Jordan Lee"}

THE_ASK = "please update my city from SFO to Irvine and house no 1429 Silverstein"


class Conversation:
    """Turns taken one after another, carrying the state as a client would."""

    def __init__(self, available=None, current=None, **kwargs):
        self.available = available or BOTH
        self.current = ON_FILE if current is None else current
        self.kwargs = kwargs
        self.state = None
        self.reply = None

    def send(self, given, **context):
        body = {"input": given, "state": self.state,
                "context": {"current": self.current, **context}}
        self.reply = take_turn(self.available, body, **self.kwargs)
        self.state = self.reply["state"]
        return self.reply

    def say(self, text, **extra):
        return self.send({"type": "text", "text": text, **extra})

    def click(self, action_id):
        return self.send({"type": "action", "action": action_id})

    def report(self, event, **detail):
        return self.send({"type": "event", "event": event, "detail": detail})

    def text(self):
        return " ".join(m["text"] for m in self.reply["messages"])

    def actions(self):
        return [a["id"] for a in self.reply["actions"]]

    def row(self, name):
        return next(r for r in self.reply["form"]["fields"] if r["name"] == name)


# ----------------------------------------------------------------------
# templates
# ----------------------------------------------------------------------


class TestTemplates(unittest.TestCase):
    def test_the_starters_load_and_round_trip(self):
        for template in STARTERS.values():
            with self.subTest(template.key):
                again = Template.from_dict(template.to_dict())
                self.assertEqual(again, template)

    def test_a_key_is_a_stable_slug(self):
        body = ADDRESS.to_dict()
        for bad in ("", "Address Change", "1address", "address-change", "x" * 70):
            with self.subTest(bad):
                body["key"] = bad
                with self.assertRaises(TemplateError):
                    Template.from_dict(body)

    def test_a_template_is_refused_rather_than_half_loaded(self):
        cases = {
            "no fields": {"key": "x", "fields": []},
            "unknown semantic type": {"key": "x", "fields": [
                {"name": "a", "semantic_type": "shoe_size"}]},
            "two fields with one name": {"key": "x", "fields": [
                {"name": "a"}, {"name": "a"}]},
            "follows a stranger": {"key": "x", "fields": [
                {"name": "a", "follows": ["b"]}]},
            "an unknown action": {"key": "x", "fields": [{"name": "a"}],
                                  "actions": ["email"]},
            "a model id that is not one": {"key": "x", "fields": [{"name": "a"}],
                                           "model_id": "sch-1"},
            "a newer major version": {"key": "x", "fields": [{"name": "a"}],
                                      "template_version": "2.0"},
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(TemplateError):
                Template.from_dict(body)

    def test_a_field_is_known_by_its_label_aliases_and_kind(self):
        street = ADDRESS.field("street_address")
        words = street.words()
        for word in ("street address", "house no", "house number", "address"):
            self.assertIn(word, words)
        self.assertEqual(words, sorted(words, key=lambda w: (-len(w), w)),
                         "longest first, so 'zip code' wins over 'zip'")

    def test_a_schema_becomes_a_draft_without_what_nobody_says_aloud(self):
        schema = FormSchema(name="Policy change", fields=[
            Field(name="policy_number", label="Policy #", semantic_type="policy_number",
                  constraints=Constraints(required=True)),
            Field(name="plan", label="Plan", semantic_type="enum", control="select",
                  options=[Option(value="Gold"), Option(value="Silver")]),
            Field(name="password", label="Password", semantic_type="password"),
            Field(name="notes", label="Notes", semantic_type="free_text"),
            Field(name="agent", label="Agent", constraints=Constraints(read_only=True)),
        ])
        draft = from_schema(schema)
        self.assertEqual(draft.key, "policy_change")
        self.assertEqual(draft.names(), ["policy_number", "plan"])
        self.assertTrue(draft.field("policy_number").required)
        self.assertEqual(draft.field("plan").options, ["Gold", "Silver"])


class TestTemplatesInTheLibrary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fillerai-bot-")
        self.store = Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_saving_the_same_key_replaces_rather_than_duplicates(self):
        first = templates.save(self.store, copy.deepcopy(ADDRESS))
        changed = copy.deepcopy(ADDRESS)
        changed.examples.append("we moved house")
        second = templates.save(self.store, changed)
        self.assertNotEqual(first.entry_id, second.entry_id)
        listed = templates.listed(self.store)
        self.assertEqual([t.key for t in listed], ["address_change"])
        self.assertIn("we moved house", listed[0].examples)
        self.assertFalse(self.store.has(first.entry_id))

    def test_a_template_is_found_and_removed_by_key(self):
        templates.save(self.store, copy.deepcopy(ADDRESS))
        templates.save(self.store, copy.deepcopy(DOCUMENT))
        self.assertEqual(templates.find(self.store, "document_request").name,
                         "Document request")
        self.assertTrue(templates.remove(self.store, "document_request"))
        self.assertIsNone(templates.find(self.store, "document_request"))
        self.assertEqual(self.store.totals()["template"], 1)


# ----------------------------------------------------------------------
# reading a phrase
# ----------------------------------------------------------------------


class TestReading(unittest.TestCase):
    def read(self, text, active=None, **kwargs):
        reading = understand.read(BOTH, active, text, **kwargs)
        return understand.decide(reading.scores)[0], understand.plain(reading.values)

    def test_the_phrase_from_the_ask(self):
        key, values = self.read(THE_ASK)
        self.assertEqual(key, "address_change")
        self.assertEqual(values, {"city": "Irvine", "street_address": "1429 Silverstein"})
        found = understand.read_values(ADDRESS, THE_ASK)[0]
        self.assertEqual(found["city"].said_before, "SFO")

    def test_an_address_written_as_an_address(self):
        key, values = self.read("I moved to 12 Main St, apt 4B, Austin, Texas 78701")
        self.assertEqual(key, "address_change")
        self.assertEqual(values, {"street_address": "12 Main St", "unit": "4B",
                                  "city": "Austin", "state": "TX",
                                  "postal_code": "78701"})

    def test_a_state_code_is_not_read_as_a_country_code(self):
        _, values = self.read("new address is 88 Ocean Ave, Santa Monica CA 90401")
        self.assertEqual(values.get("state"), "CA")
        self.assertNotIn("country", values)
        self.assertEqual(values.get("city"), "Santa Monica")

    def test_the_other_template_and_its_options(self):
        key, values = self.read("I need my ID card for policy PA-1048822, send it by email")
        self.assertEqual(key, "document_request")
        self.assertEqual(values, {"policy_number": "PA-1048822",
                                  "document_type": "ID card", "delivery": "Email"})

    def test_nothing_matching_is_said_rather_than_guessed(self):
        self.assertEqual(self.read("hello there"), (None, {}))

    def test_two_templates_too_close_to_call_come_back_as_a_choice(self):
        twin = copy.deepcopy(DOCUMENT)
        twin.key, twin.name = "document_copy", "Document copy"
        reading = understand.read([DOCUMENT, twin], None, "request a document")
        key, choices = understand.decide(reading.scores)
        self.assertIsNone(key)
        self.assertEqual(set(choices), {"document_request", "document_copy"})

    def test_an_answer_to_the_question_just_asked(self):
        found, _ = understand.read_values(ADDRESS, "92618", expects="postal_code")
        self.assertEqual(understand.plain(found), {"postal_code": "92618"})

    def test_an_answer_to_a_different_question_lands_where_it_fits(self):
        found, problems = understand.read_values(ADDRESS, "92618", expects="state")
        self.assertEqual(understand.plain(found), {"postal_code": "92618"})
        self.assertEqual(problems, [])

    def test_a_correction_goes_to_the_field_mentioned_last(self):
        found, _ = understand.read_values(ADDRESS, "actually make it Tustin",
                                          last_field="city")
        self.assertEqual(understand.plain(found), {"city": "Tustin"})

    def test_what_may_go_in_a_field_is_decided_in_one_place(self):
        state, country = ADDRESS.field("state"), ADDRESS.field("country")
        self.assertEqual(understand.accept(state, "california"), ("CA", ""))
        self.assertIsNone(understand.accept(state, "92618")[0])
        self.assertEqual(understand.accept(country, "usa"), ("US", ""))
        self.assertIsNone(understand.accept(country, "France")[0],
                          "not on this template's list")
        kind = DOCUMENT.field("document_type")
        self.assertEqual(understand.accept(kind, "id card")[0], "ID card")

    def test_whole_phrases_are_actions_and_sentences_are_not(self):
        self.assertEqual(understand.action_phrase("Submit"), "submit")
        self.assertEqual(understand.action_phrase("yes please"), "confirm")
        self.assertEqual(understand.action_phrase("Fill the form for manual submission"),
                         "fill_form")
        self.assertEqual(understand.action_phrase("start over"), "cancel")
        self.assertIsNone(understand.action_phrase("submit a claim for my car"))


# ----------------------------------------------------------------------
# the turn
# ----------------------------------------------------------------------


class TestTheTurn(unittest.TestCase):
    def test_the_ask_end_to_end(self):
        """§3: template, values, before/after, and the question that is left."""
        chat = Conversation()
        reply = chat.say(THE_ASK, via="speech")
        self.assertEqual(reply["intent"]["template"], "address_change")
        self.assertTrue(reply["intent"]["changed"])
        self.assertEqual(reply["form"]["changes"]["city"],
                         {"before": "San Francisco", "after": "Irvine"})
        self.assertEqual(chat.row("city")["said_before"], "SFO")
        self.assertEqual(chat.row("street_address")["after"], "1429 Silverstein")
        self.assertEqual(reply["understood"], {"text": THE_ASK, "via": "speech"})
        # A new city makes the state and ZIP on file wrong: asked, not kept.
        self.assertEqual(chat.row("postal_code")["status"], "outdated")
        self.assertEqual(reply["form"]["missing"], ["state", "postal_code"])
        self.assertEqual(reply["expects"]["field"], "state")
        self.assertNotIn("submit", chat.actions())
        self.assertIn("fill_form", chat.actions())
        self.assertEqual(chat.row("country")["status"], "kept")

        chat.say("CA")
        self.assertEqual(chat.row("state")["status"], "unchanged")
        reply = chat.say("92618")
        self.assertTrue(reply["form"]["complete"])
        self.assertEqual(reply["conversation"]["status"], "ready")
        self.assertEqual(chat.actions(), ["submit", "fill_form", "cancel"])
        self.assertEqual(reply["actions"][0]["style"], "primary")
        self.assertEqual(reply["actions"][1]["label"], "Fill the form for manual submission")

    def test_typing_submit_and_clicking_submit_are_the_same_turn(self):
        """§3: all three inputs arrive the same way."""
        replies = []
        for how in ("typed", "spoken", "clicked"):
            chat = Conversation()
            chat.say("change my address to 12 Main St, Austin, Texas 78701")
            if how == "clicked":
                reply = chat.click("submit")
            else:
                reply = chat.say("submit it", via="speech" if how == "spoken" else "typed")
            replies.append((reply["effect"], reply["conversation"]["status"],
                            reply["messages"]))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0], replies[2])
        effect = replies[0][0]
        self.assertEqual(effect["type"], "submit")
        self.assertEqual(effect["values"]["city"], "Austin")
        self.assertEqual(effect["values"]["country"], "US", "kept values go too")

    def test_yes_means_the_primary_action(self):
        chat = Conversation()
        chat.say("change my address to 12 Main St, Austin, Texas 78701")
        self.assertEqual(chat.say("yes")["effect"]["type"], "submit")

    def test_asking_to_submit_too_early_says_what_is_missing(self):
        chat = Conversation()
        chat.say(THE_ASK)
        reply = chat.say("submit")
        self.assertIsNone(reply["effect"])
        self.assertIn("I still need the state and ZIP code", chat.text())

    def test_fill_form_hands_the_values_over_and_ends_the_conversation(self):
        chat = Conversation()
        chat.say(THE_ASK)
        reply = chat.click("fill_form")
        self.assertEqual(reply["effect"]["type"], "fill_form")
        self.assertEqual(reply["effect"]["changes"]["city"]["after"], "Irvine")
        self.assertNotIn("postal_code", reply["effect"]["values"],
                         "an outdated value is not handed over as if it held")
        self.assertIn("still needs", chat.text())
        self.assertEqual(reply["conversation"]["status"], "handed_off")
        self.assertEqual(chat.report("filled")["conversation"]["status"], "handed_off")

    def test_the_host_reports_back_after_a_submit(self):
        """§3.3."""
        chat = Conversation()
        chat.say("change my address to 12 Main St, Austin, Texas 78701")
        chat.click("submit")
        failed = chat.report("submit_failed", message="the service is down")
        self.assertEqual(failed["conversation"]["status"], "ready")
        self.assertIn("the service is down", chat.text())
        self.assertIn("submit", chat.actions())
        chat.click("submit")
        done = chat.report("submitted", reference="CHG-7")
        self.assertEqual(done["conversation"]["status"], "done")
        self.assertIn("Your address change is in. Your reference is CHG-7.", chat.text())

    def test_a_stale_or_repeated_click_is_refused(self):
        """A double click on an old Submit must not submit twice."""
        chat = Conversation()
        chat.say("change my address to 12 Main St, Austin, Texas 78701")
        chat.click("submit")
        with self.assertRaises(BotError) as caught:
            chat.click("submit")
        self.assertEqual(caught.exception.code, "stale_action")
        self.assertEqual(caught.exception.status, 409)

    def test_a_field_with_few_options_is_asked_as_buttons(self):
        chat = Conversation(current={"policy_number": "PA-1", "full_name": "Jo Lee"})
        reply = chat.say("I need a document")
        self.assertEqual(reply["expects"]["field"], "document_type")
        answers = [a for a in reply["actions"] if a["type"] == "answer"]
        self.assertEqual([a["value"] for a in answers], DOCUMENT.field("document_type").options)
        reply = chat.click(answers[1]["id"])
        self.assertEqual(chat.row("document_type"),
                         {**chat.row("document_type"), "after": "ID card", "source": "clicked"})
        self.assertEqual(reply["conversation"]["status"], "ready")
        self.assertEqual(chat.actions(), ["submit", "cancel"],
                         "this template only submits")

    def test_what_the_host_holds_is_used_not_asked_for(self):
        chat = Conversation(current={"policy_number": "PA-1", "full_name": "Jo Lee"})
        reply = chat.say("I need my declarations page")
        self.assertTrue(reply["form"]["complete"])
        self.assertEqual(chat.row("policy_number")["status"], "kept")

    def test_an_unclear_opening_offers_the_templates(self):
        chat = Conversation()
        reply = chat.say("hello")
        self.assertIsNone(reply["intent"]["template"])
        self.assertEqual(chat.actions(), ["choose:address_change", "choose:document_request"])
        reply = chat.click("choose:address_change")
        self.assertEqual(reply["intent"]["how"], "chosen")

    def test_a_choice_reads_what_was_said_before_it(self):
        twin = copy.deepcopy(ADDRESS)
        twin.key, twin.name = "address_fix", "Address fix"
        chat = Conversation(available=[ADDRESS, twin])
        chat.say("update my address, city Irvine")
        self.assertIn("choose:address_fix", chat.actions())
        reply = chat.click("choose:address_fix")
        self.assertEqual(reply["form"]["changes"]["city"]["after"], "Irvine")

    def test_a_clear_change_of_subject_switches_and_sets_the_first_aside(self):
        chat = Conversation(current={})
        chat.say("change my city to Irvine")
        reply = chat.say("actually I need a copy of my policy first")
        self.assertEqual(reply["intent"]["template"], "document_request")
        self.assertTrue(reply["intent"]["changed"])
        self.assertIn("set the address change aside", chat.text())
        reply = chat.say("I'd like to update my address")
        self.assertEqual(reply["form"]["values"].get("city"), "Irvine",
                         "the parked values come back")

    def test_more_information_is_not_a_change_of_subject(self):
        chat = Conversation()
        chat.say(THE_ASK)
        reply = chat.say("and the unit is 4B")
        self.assertFalse(reply["intent"]["changed"])
        self.assertEqual(reply["form"]["values"]["unit"], "4B")

    def test_start_over_clears_it(self):
        chat = Conversation()
        chat.say(THE_ASK)
        reply = chat.say("start over")
        self.assertEqual(reply["conversation"]["status"], "cancelled")
        self.assertIsNone(reply["form"])
        reply = chat.say("I need my ID card")
        self.assertEqual(reply["intent"]["template"], "document_request")

    def test_a_finished_conversation_spoken_to_again_starts_anew(self):
        chat = Conversation()
        chat.say("change my address to 12 Main St, Austin, Texas 78701")
        chat.click("submit")
        conversation = chat.report("submitted")["conversation"]["id"]
        reply = chat.say("I need my ID card")
        self.assertEqual(reply["conversation"]["id"], conversation)
        self.assertEqual(reply["intent"]["template"], "document_request")

    def test_a_host_can_start_on_a_template(self):
        chat = Conversation()
        reply = chat.send({"type": "text", "text": "12 Main St"},
                          template="address_change")
        self.assertEqual(reply["intent"]["how"], "context")
        self.assertEqual(chat.row("street_address")["after"], "12 Main St")

    def test_speech_alternatives_are_tried_when_the_first_reading_is_empty(self):
        chat = Conversation()
        reply = chat.say("up date mile dress", via="speech",
                         alternatives=["update my address"])
        self.assertEqual(reply["intent"]["template"], "address_change")

    def test_a_linked_model_completes_what_was_not_said(self):
        linked = copy.deepcopy(ADDRESS)
        linked.model_id = "mdl-20260101-000000000-abcd"
        asked = []

        class Guess:
            def __init__(self, value, confidence):
                self.value, self.confidence, self.known = value, confidence, True

        class Model:
            profiles = {"city": 1, "state": 1, "postal_code": 1}

            def predict(self, observed):
                asked.append(dict(observed))
                return {"state": Guess("CA", 0.93), "postal_code": Guess("92618", 0.4)}

        chat = Conversation(available=[linked],
                            complete=model_completer(lambda model_id: Model()))
        chat.say("change my city to Irvine")
        self.assertEqual(asked, [{"city": "Irvine"}])
        self.assertEqual(chat.row("state")["source"], "model")
        self.assertEqual(chat.row("state")["confidence"], 0.93)
        self.assertEqual(chat.row("postal_code")["status"], "outdated",
                         "below the threshold is not offered")


class TestTheStateIsNotTrusted(unittest.TestCase):
    """§4: the state passes through the end user's browser."""

    def tampered(self, change):
        chat = Conversation()
        chat.say(THE_ASK)
        state = copy.deepcopy(chat.state)
        change(state)
        chat.state = state
        with self.assertRaises(BotError) as caught:
            chat.say("CA")
        return caught.exception

    def test_a_field_the_template_does_not_have(self):
        error = self.tampered(lambda s: s["values"].update(
            {"is_admin": {"value": "yes", "source": "said"}}))
        self.assertEqual(error.code, "bad_state")

    def test_a_value_the_field_cannot_hold(self):
        error = self.tampered(lambda s: s["values"].update(
            {"country": {"value": "Atlantis", "source": "said"}}))
        self.assertEqual(error.code, "bad_state")

    def test_a_template_this_chat_may_not_use(self):
        chat = Conversation()
        chat.say("I need my ID card")
        chat.available = [ADDRESS]
        with self.assertRaises(BotError) as caught:
            chat.say("PA-1")
        self.assertEqual(caught.exception.code, "template_not_allowed")

    def test_something_that_is_not_a_state_at_all(self):
        for bad in ("a string", {"v": 99}, {"v": 1, "id": "nope"}):
            with self.subTest(bad):
                with self.assertRaises(BotError) as caught:
                    take_turn(BOTH, {"input": {"type": "text", "text": "hi"},
                                     "state": bad})
                self.assertEqual(caught.exception.code, "bad_state")

    def test_inputs_have_one_of_three_shapes(self):
        for bad in (None, {"type": "audio"}, {"type": "text", "text": "  "},
                    {"type": "text", "text": "x", "via": "carrier pigeon"},
                    {"type": "event", "event": "exploded"},
                    {"type": "action"}):
            with self.subTest(bad):
                with self.assertRaises(BotError) as caught:
                    take_turn(BOTH, {"input": bad})
                self.assertEqual(caught.exception.code, "bad_input")

    def test_no_templates_is_said_plainly(self):
        with self.assertRaises(BotError) as caught:
            take_turn([], {"input": {"type": "text", "text": "hi"}})
        self.assertEqual(caught.exception.code, "no_templates")



class TestTheCommand(unittest.TestCase):
    def test_add_list_and_remove(self):
        import contextlib
        import io

        from fillerai import cli

        folder = tempfile.mkdtemp(prefix="fillerai-bot-cli-")
        self.addCleanup(shutil.rmtree, folder, True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["bot", "--library", folder, "add",
                                       "--starter", "address_change"]), 0)
            self.assertEqual(cli.main(["bot", "--library", folder, "list"]), 0)
            self.assertEqual(cli.main(["bot", "--library", folder, "remove",
                                       "address_change"]), 0)
        self.assertIn("address_change           Address change", out.getvalue())
        self.assertEqual(templates.listed(Store(folder)), [])


if __name__ == "__main__":
    unittest.main()
