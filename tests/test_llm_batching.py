"""Asking a form too large to ask about all at once.

The bug these were written for: ``propose-rules`` sized every request the
same way regardless of the form, and a large form's answer ran out of room
part-way through. The model stopped mid-sentence, the reply came back with
``stop_reason: max_tokens``, and the command reported "the answer was cut off
by the token limit" with nothing anybody could do about it but use a smaller
form.

Two things were wrong, and both are covered here. The budget was a constant
that did not know how many rules it had asked for, and - the part that is
invisible unless you go looking - it made no allowance for a model that
reasons before it answers, which the models these tasks default to all do,
out of the same budget. And there was no upper end: past some size no single
answer fits however generous the budget, so the question itself has to be
split.

Nothing here reaches a network. :class:`Counting` stands in for the client and
records what it was asked, which is exactly what is under test: how many calls
were made, how large each was allowed to be, and what each one asked about.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fillerai
from fillerai.llm import prompts
from fillerai.llm import rules as llm_rules
from fillerai.llm.config import Settings
from fillerai.llm.providers import Reply, ReplyError
from fillerai.schema import Field, FormSchema, Option

FORM = ROOT / "examples" / "member_enrollment.fields.json"

SETTINGS = Settings.resolve("rules", environ={"FILLERAI_LLM_KEY": "not-a-real-key"})


def big_form(options: int = 130, plain: int = 40) -> FormSchema:
    """A form with more fields that could carry a rule than one answer holds."""
    fields = [Field(name=f"plain_{i}", label=f"Plain {i}", control="text")
              for i in range(plain)]
    fields += [
        Field(name=f"pick_{i}", label=f"Pick {i}", control="select",
              options=[Option("A"), Option("B"), Option("C")])
        for i in range(options)
    ]
    return FormSchema(name="huge", fields=fields)


class Counting:
    """A client that answers nothing and remembers everything it was asked."""

    def __init__(self, truncate: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        #: 1-based call numbers to fail as a cut-off answer.
        self.truncate = truncate or set()

    def ask(self, *, system, user, output_schema=None, max_tokens=8000) -> Reply:
        self.calls.append({"user": user, "max_tokens": max_tokens})
        if len(self.calls) in self.truncate:
            raise ReplyError("cut off", truncated=True)
        return Reply(text="{}", model="stub", stop_reason="end_turn",
                     usage={"input_tokens": 10, "output_tokens": 5},
                     data={"rules": []})

    def asked_about(self, call: int) -> list[str]:
        """The fields the n-th call was told to decide, in order."""
        lines = self.calls[call]["user"].splitlines()
        return [line.strip() for line in lines
                if line.startswith("  ") and line.strip().startswith("pick_")]


class TestSizingOneAnswer(unittest.TestCase):
    """What the budget has to be for the answer to fit in it."""

    def test_the_budget_covers_the_rules_it_asked_for_and_room_to_think(self):
        """The regression itself: never ask for more than you allow."""
        for count in (1, 10, 25, llm_rules.BATCH_TARGETS):
            with self.subTest(count=count):
                needed = count * prompts.TOKENS_PER_RULE
                self.assertGreaterEqual(
                    prompts.rules_budget(count),
                    needed + prompts.THINKING_ALLOWANCE)

    def test_a_tiny_form_still_gets_room_to_think(self):
        self.assertEqual(prompts.rules_budget(1), prompts.MIN_OUTPUT_TOKENS)

    def test_the_budget_grows_with_the_question(self):
        self.assertGreater(prompts.rules_budget(400), prompts.rules_budget(1))

    def test_no_single_call_is_allowed_to_be_unbounded(self):
        self.assertEqual(prompts.rules_budget(100_000), prompts.MAX_OUTPUT_TOKENS)

    def test_a_batch_of_the_default_size_is_sent_with_the_sized_budget(self):
        client = Counting()
        llm_rules.propose(big_form(), client=client, settings=SETTINGS)
        wanted = prompts.rules_budget(llm_rules.BATCH_TARGETS)
        self.assertTrue(all(c["max_tokens"] >= wanted for c in client.calls))


class TestWhatCouldCarryARule(unittest.TestCase):
    """Sizing a run by the form's field count overstates it badly."""

    def test_a_field_with_no_options_is_not_a_target(self):
        schema = big_form(options=3, plain=50)
        self.assertEqual(llm_rules.targets(schema),
                         ["pick_0", "pick_1", "pick_2"])

    def test_a_field_that_already_has_a_rule_is_not_a_target(self):
        schema = fillerai.extract_spec(
            ROOT / "examples" / "auto_insurance_quote.fields.json")
        declared = {f.name for f in schema.fields if f.derived}
        self.assertTrue(declared)
        self.assertFalse(declared & set(llm_rules.targets(schema)))


class TestSplittingTheQuestion(unittest.TestCase):
    def test_a_form_that_fits_is_one_group(self):
        schema = fillerai.extract_spec(FORM)
        self.assertEqual(len(llm_rules.batches(schema)), 1)

    def test_a_form_exactly_at_the_limit_is_still_one_group(self):
        schema = big_form(options=llm_rules.BATCH_TARGETS, plain=0)
        self.assertEqual(len(llm_rules.batches(schema)), 1)

    def test_one_more_than_the_limit_splits(self):
        schema = big_form(options=llm_rules.BATCH_TARGETS + 1, plain=0)
        self.assertEqual(len(llm_rules.batches(schema)), 2)

    def test_the_groups_together_are_the_targets_once_each(self):
        schema = big_form()
        flat = [name for group in llm_rules.batches(schema) for name in group]
        self.assertEqual(flat, llm_rules.targets(schema))

    def test_the_estimate_counts_every_call_not_just_the_first(self):
        schema = big_form()
        groups = llm_rules.batches(schema)
        self.assertGreater(len(groups), 1)
        one = llm_rules.estimate(schema, SETTINGS, size=0).input_tokens
        many = llm_rules.estimate(schema, SETTINGS).input_tokens
        self.assertGreater(many, one)


class TestAskingInSeveralCalls(unittest.TestCase):
    def setUp(self):
        self.schema = big_form()
        self.client = Counting()
        self.proposals = llm_rules.propose(
            self.schema, client=self.client, settings=SETTINGS)

    def test_one_call_per_group(self):
        self.assertEqual(len(self.client.calls),
                         len(llm_rules.batches(self.schema)))

    def test_each_call_is_asked_about_its_own_slice_and_no_other(self):
        seen: list[str] = []
        for index in range(len(self.client.calls)):
            asked = self.client.asked_about(index)
            self.assertTrue(asked)
            seen.extend(asked)
        self.assertEqual(seen, llm_rules.targets(self.schema))

    def test_every_call_still_shows_the_whole_form(self):
        """A rule links two fields; half a form invents links inside the half."""
        for call in self.client.calls:
            for field in self.schema.fields:
                self.assertIn(field.name, call["user"])

    def test_the_run_says_how_many_calls_it_took(self):
        self.assertEqual(self.proposals.usage["calls"], len(self.client.calls))

    def test_a_form_that_fits_is_asked_the_undivided_question(self):
        client = Counting()
        llm_rules.propose(fillerai.extract_spec(FORM), client=client,
                          settings=SETTINGS, sample=20)
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("these fields only", client.calls[0]["user"])


class TestACutOffAnswerIsAskedAgainSmaller(unittest.TestCase):
    """The failure that started this, handled rather than reported."""

    def test_a_truncated_group_is_halved_and_asked_twice(self):
        client = Counting(truncate={1})
        llm_rules.propose(big_form(), client=client, settings=SETTINGS)
        halves = client.asked_about(1), client.asked_about(2)
        first = llm_rules.batches(big_form())[0]
        self.assertEqual(list(halves[0]) + list(halves[1]), first)

    def test_it_gives_up_rather_than_halving_forever(self):
        client = Counting(truncate=set(range(1, 200)))
        with self.assertRaises(ReplyError):
            llm_rules.propose(big_form(), client=client, settings=SETTINGS)
        # The first group, halved SPLIT_DEPTH times and still cut off, is the
        # end of it: the failure comes back rather than dividing forever.
        self.assertEqual(len(client.calls), llm_rules.SPLIT_DEPTH + 1)

    def test_a_failure_that_is_not_truncation_is_reported_not_retried(self):
        class Refusing(Counting):
            def ask(self, **kw):
                super().ask(**kw)
                raise ReplyError("the model declined to answer this request")

        client = Refusing()
        with self.assertRaises(ReplyError):
            llm_rules.propose(big_form(), client=client, settings=SETTINGS)
        self.assertEqual(len(client.calls), 1)

    def test_an_undivided_question_that_truncates_is_reported(self):
        """There is no smaller question to ask, so the reader hears about it."""
        client = Counting(truncate={1})
        with self.assertRaises(ReplyError):
            llm_rules.propose(fillerai.extract_spec(FORM), client=client,
                              settings=SETTINGS)

    def test_the_message_says_what_to_do_about_it(self):
        from fillerai.llm import providers

        self.assertIn("--batch", providers.TRUNCATED)


class TestTwoProposalsForOneField(unittest.TestCase):
    """Asking in slices means a model can answer outside the slice it was given."""

    def setUp(self):
        self.schema = fillerai.extract_spec(FORM)

    def rule(self, confidence: float) -> llm_rules.Proposal:
        return llm_rules.Proposal.from_payload({
            "field": "home_country", "follows": ["home_state"],
            "when": {"CA": "US", "TX": "US"},
            "confidence": confidence, "why": "because",
        })

    def test_the_more_confident_one_survives(self):
        verdicts = llm_rules.check(
            self.schema, [self.rule(0.6), self.rule(0.9)], sample=40)
        kept = [v for v in verdicts if v.kept]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].proposal.confidence, 0.9)

    def test_the_one_that_went_says_why(self):
        verdicts = llm_rules.check(
            self.schema, [self.rule(0.9), self.rule(0.6)], sample=40)
        dropped = [v for v in verdicts if not v.kept]
        self.assertEqual(len(dropped), 1)
        self.assertIn("more confident (0.90)", dropped[0].problem)


if __name__ == "__main__":
    unittest.main()
