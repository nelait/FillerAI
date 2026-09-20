"""Tests for the simulation stage. Run with: python -m unittest discover -s tests

Three things are worth holding still here. The form drawn from a schema has
to be the form the schema describes - same fields, same order, same screens,
same dropdowns. The board has to classify every field honestly, including the
ones the model refuses. And the saving has to be arithmetic on stated
assumptions rather than a number that flatters the project: every test below
that touches a total exists because the easy version of that total would have
overstated it.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fillerai
from fillerai.cli import main
from fillerai.schema import Constraints, Field, FormSchema, Option, Screen
from fillerai.simulate import effort as effort_mod
from fillerai.simulate.effort import DEFAULT_EFFORT, Effort, spell_out
from fillerai.simulate.form import layout
from fillerai.simulate.run import (
    ASSUMED_LENGTH,
    FILLED,
    SUGGESTED,
    TYPED,
    YOURS,
    run,
    sweep,
)
from fillerai.train.model import train

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Above anything a calibrated confidence reaches, so a test can ask for the
# "nothing is sure enough to fill" case without guessing at a number the
# calibration might move.
CERTAIN = 0.999


def field(name: str, **kwargs) -> Field:
    constraints = kwargs.pop("constraints", None) or Constraints()
    return Field(name=name, constraints=constraints, **kwargs)


# A tiny form with one real relationship in it: the state follows the city.
# Small enough to reason about, so every number below can be checked by hand.
CITIES = [("Austin", "TX"), ("Dallas", "TX"), ("Reno", "NV")]


def tiny_schema() -> FormSchema:
    return FormSchema(name="tiny", fields=[
        field("city", label="City"),
        field("state", label="State", control="select",
              options=[Option(value=s) for s in ("TX", "NV")]),
        field("country", label="Country"),
        field("ref", label="Reference"),
    ])


def tiny_records(count: int = 60) -> list[dict]:
    rows = []
    for index in range(count):
        city, state = CITIES[index % len(CITIES)]
        rows.append({"city": city, "state": state, "country": "US",
                     "ref": f"R-{index:05d}"})
    return rows


def tiny_model():
    return train(tiny_schema(), tiny_records())


class TestLayout(unittest.TestCase):
    def test_the_form_drawn_is_the_form_the_schema_describes(self):
        schema = fillerai.extract_html(EXAMPLES / "claims_intake.html")
        drawn = layout(schema)
        fillable = [f.name for f in schema.fields if not f.constraints.read_only]
        # Same fields, and crucially the same order: the source's order is the
        # order the form was designed to be filled in.
        self.assertEqual(drawn.names(), fillable)
        self.assertEqual(drawn.field_count, len(fillable))
        self.assertEqual([p.id for p in drawn.pages],
                         [s.id for s in schema.screens])

    def test_a_read_only_field_is_never_drawn(self):
        schema = FormSchema(name="t", fields=[
            field("typed_in"),
            field("computed", constraints=Constraints(read_only=True)),
        ])
        self.assertEqual(layout(schema).names(), ["typed_in"])

    def test_groups_stay_where_the_form_put_them(self):
        # A form that returns to an earlier group late on is drawn that way,
        # rather than tidied into one section per group name - the tidy
        # version would not match the screen the agent is looking at.
        schema = FormSchema(name="t", fields=[
            field("a", group="home"), field("b", group="work"),
            field("c", group="home"),
        ])
        sections = layout(schema).pages[0].sections
        self.assertEqual([s.group for s in sections], ["home", "work", "home"])

    def test_a_dropdown_keeps_its_options_and_a_number_keeps_its_limits(self):
        schema = FormSchema(name="t", fields=[
            field("pick", control="select",
                  options=[Option(value="a", label="A"), Option(value="b")]),
            field("amount", control="number",
                  constraints=Constraints(minimum=0, maximum=50, step=0.5)),
        ])
        pick, amount = layout(schema).pages[0].sections[0].controls
        self.assertEqual(pick.control, "select")
        self.assertEqual([o["value"] for o in pick.options], ["a", "b"])
        self.assertEqual(pick.options[0]["label"], "A")
        self.assertEqual(amount.control, "input")
        self.assertEqual(amount.input_type, "number")
        # ``minimum: 0`` is the constraint most easily lost to a falsy check,
        # and losing it lets the box accept anything.
        self.assertEqual(amount.minimum, 0)
        self.assertEqual(amount.maximum, 50)

    def test_a_control_this_build_has_never_heard_of_becomes_a_text_box(self):
        # Which is what a browser does with an unknown input type. Dropping
        # the field instead would quietly shorten the form.
        schema = FormSchema(name="t", fields=[field("odd", control="quantum")])
        control = layout(schema).pages[0].sections[0].controls[0]
        self.assertEqual(control.control, "input")
        self.assertEqual(control.input_type, "text")

    def test_options_without_a_control_to_show_them_become_a_dropdown(self):
        # A field spec can name choices without naming a control. A text box
        # would throw the choices away.
        schema = FormSchema(name="t", fields=[
            field("pick", options=[Option(value="a"), Option(value="b")]),
        ])
        self.assertEqual(layout(schema).pages[0].sections[0].controls[0].control,
                         "select")

    def test_a_screen_keeps_its_title_and_an_empty_form_still_draws(self):
        schema = FormSchema(name="t", fields=[field("a", screen="s1")],
                            screens=[Screen(id="s1", title="Step one")])
        self.assertEqual(layout(schema).pages[0].title, "Step one")
        # Nothing to draw is a form with no fields, not a crash.
        self.assertEqual(layout(FormSchema(name="empty")).field_count, 0)


class TestEffort(unittest.TestCase):
    def test_choosing_from_a_dropdown_does_not_depend_on_the_value(self):
        # Picking "Massachusetts" is the same work as picking "Ohio", and
        # charging per character would invent a saving out of long labels.
        long_pick = DEFAULT_EFFORT.type_cost("select", 40)
        short_pick = DEFAULT_EFFORT.type_cost("select", 2)
        self.assertEqual(long_pick, short_pick)
        self.assertEqual(long_pick[0], 1)

    def test_typing_costs_the_overhead_plus_the_characters(self):
        effort = Effort(chars_per_second=10.0, seconds_per_field=1.0)
        keystrokes, seconds = effort.type_cost("text", 20)
        self.assertEqual(keystrokes, 20)
        self.assertAlmostEqual(seconds, 1.0 + 2.0)

    def test_a_wrong_fill_costs_more_than_an_empty_box(self):
        # The whole reason the model declines a field it cannot predict.
        _, typing = DEFAULT_EFFORT.type_cost("text", 12)
        _, fixing = DEFAULT_EFFORT.correct_cost("text", 12)
        self.assertGreater(fixing, typing)

    def test_seconds_are_spelled_the_way_a_person_says_them(self):
        self.assertEqual(spell_out(8), "8s")
        self.assertEqual(spell_out(160), "2m 40s")
        self.assertEqual(spell_out(4320), "1h 12m")
        self.assertEqual(spell_out(-5), "0s")

    def test_the_assumptions_are_printable_rather_than_buried(self):
        self.assertEqual(len(DEFAULT_EFFORT.assumptions()), 5)
        self.assertIn("chars_per_second", DEFAULT_EFFORT.to_dict())


class TestRun(unittest.TestCase):
    def setUp(self):
        self.model = tiny_model()

    def test_every_field_lands_in_exactly_one_of_the_four_states(self):
        result = run(self.model, {"city": "Austin"})
        by_name = {c.name: c for c in result.cells}
        self.assertEqual(by_name["city"].source, TYPED)
        self.assertEqual(by_name["state"].source, FILLED)
        self.assertEqual(by_name["state"].value, "TX")
        # A reference that is different in every record is not a gap the
        # model failed to close; it is the answer.
        self.assertEqual(by_name["ref"].source, YOURS)
        self.assertIsNone(by_name["ref"].value or None)
        self.assertEqual(len(result.cells), len(self.model.targets()))

    def test_the_reason_beside_a_filled_value_names_the_field_it_came_from(self):
        state = next(c for c in run(self.model, {"city": "Reno"}).cells
                     if c.name == "state")
        self.assertEqual(state.value, "NV")
        self.assertIn("city", " ".join(state.because))

    def test_a_prediction_below_the_bar_is_shown_but_not_filled_in(self):
        high = run(self.model, {"city": "Austin"}, threshold=CERTAIN)
        state = next(c for c in high.cells if c.name == "state")
        self.assertEqual(state.source, SUGGESTED)
        self.assertEqual(state.value, "TX")
        # It is still the agent's to type, so it counts against the saving.
        self.assertEqual(high.savings.filled, 0)
        self.assertIn("state", [c.name for c in high.cells])
        self.assertNotIn("state", high.record())

    def test_the_submitted_record_is_what_was_typed_and_what_was_filled(self):
        result = run(self.model, {"city": "Dallas"})
        self.assertEqual(result.record(), {"city": "Dallas", "state": "TX",
                                           "country": "US"})

    def test_a_case_marks_each_filled_value_right_or_wrong(self):
        case = {"city": "Austin", "state": "TX", "country": "US", "ref": "R-99999"}
        result = run(self.model, {"city": "Austin"}, case=case)
        state = next(c for c in result.cells if c.name == "state")
        self.assertEqual(state.verdict, "right")
        # State and country are both filled, and both match.
        self.assertEqual(result.score.checked, 2)
        self.assertEqual(result.score.right, 2)
        self.assertEqual(result.score.wrong, 0)
        # The reference is in the case but the model declined it, which is
        # the work the agent genuinely still has.
        self.assertEqual(result.score.declined, 1)

    def test_a_wrong_fill_is_counted_as_wrong_and_charged_for(self):
        # The case says NV while the city says Austin, so the model's TX is
        # wrong. A run that scored this as a saving would be lying.
        case = {"city": "Austin", "state": "NV", "country": "US", "ref": "R-1"}
        wrong = run(self.model, {"city": "Austin"}, case=case)
        right = run(self.model, {"city": "Austin"},
                    case={**case, "state": "TX"})
        self.assertEqual(wrong.score.wrong, 1)
        self.assertEqual(wrong.savings.wrong, 1)
        self.assertGreater(wrong.savings.seconds_now, right.savings.seconds_now)

    def test_a_field_the_case_left_blank_is_not_scored_either_way(self):
        # A blank in the source says the field did not apply, not that the
        # model was wrong about it.
        case = {"city": "Austin", "state": "", "country": "", "ref": ""}
        result = run(self.model, {"city": "Austin"}, case=case)
        self.assertEqual(result.score.checked, 0)
        self.assertIsNone(next(c for c in result.cells
                               if c.name == "state").verdict)

    def test_held_back_values_are_scored_apart_from_filled_ones(self):
        # What moving the slider would have bought, kept separate from what
        # the model actually did.
        case = {"city": "Austin", "state": "TX", "country": "US", "ref": "R-1"}
        result = run(self.model, {"city": "Austin"}, case=case,
                     threshold=CERTAIN)
        self.assertEqual(result.score.checked, 0)
        self.assertEqual(result.score.held_back, 2)
        self.assertEqual(result.score.held_back_right, 2)

    def test_nothing_typed_still_produces_a_full_board(self):
        result = run(self.model, {})
        self.assertEqual(len(result.cells), len(self.model.targets()))
        self.assertEqual(result.savings.typed, 0)
        self.assertEqual(result.savings.fields, len(self.model.targets()))


class TestSavings(unittest.TestCase):
    """The arithmetic, which is where a saving is easiest to overstate."""

    def setUp(self):
        self.model = tiny_model()
        self.case = {"city": "Austin", "state": "TX", "country": "US",
                     "ref": "R-00042"}

    def test_the_counts_add_up_to_the_form(self):
        saving = run(self.model, {"city": "Austin"}, case=self.case).savings
        self.assertEqual(saving.typed + saving.filled + saving.left,
                         saving.fields)
        # A held-back suggestion is still the agent's to type, so it lives
        # inside ``left`` rather than beside it.
        self.assertLessEqual(saving.suggested, saving.left)

    def test_a_field_the_model_declines_costs_the_same_either_way(self):
        # No free saving on a box nobody filled. Both sides of the comparison
        # charge for it, so it cancels out instead of counting twice.
        result = run(self.model, {}, case=self.case, threshold=CERTAIN)
        self.assertEqual(result.savings.seconds_now,
                         result.savings.seconds_by_hand)
        self.assertEqual(result.savings.keystrokes_saved, 0)
        self.assertEqual(result.savings.share_saved, 0.0)

    def test_reviewing_a_filled_value_is_charged_rather_than_free(self):
        # Autofill is not free: the agent still reads what they submit.
        result = run(self.model, {"city": "Austin"}, case=self.case)
        _, review = DEFAULT_EFFORT.review_cost()
        self.assertGreater(result.savings.seconds_now, 0)
        self.assertGreater(review, 0)
        # Filling two short values cannot save the whole form's time.
        self.assertLess(result.savings.share_saved, 1.0)

    def test_the_saving_is_the_difference_between_the_two_totals(self):
        saving = run(self.model, {"city": "Austin"}, case=self.case).savings
        self.assertAlmostEqual(
            saving.seconds_saved, saving.seconds_by_hand - saving.seconds_now)
        self.assertAlmostEqual(
            saving.share_saved, saving.seconds_saved / saving.seconds_by_hand)

    def test_a_typed_field_costs_the_same_on_both_sides(self):
        # Typing a box yourself is not a saving and not a loss.
        typed_only = run(self.model, {"ref": "R-00042"}, case=self.case,
                         threshold=CERTAIN)
        self.assertEqual(typed_only.savings.seconds_now,
                         typed_only.savings.seconds_by_hand)

    def test_without_a_case_the_lengths_come_from_what_the_field_usually_holds(self):
        # There is no truth to measure against, so the by-hand cost has to be
        # estimated - and estimating it as zero would overstate the saving.
        result = run(self.model, {})
        self.assertGreater(result.savings.keystrokes_by_hand, 0)
        self.assertGreater(result.savings.seconds_by_hand, 0)

    def test_a_field_with_nothing_known_about_it_still_costs_something(self):
        schema = FormSchema(name="t", fields=[field("a"), field("b")])
        model = train(schema, [{"a": "", "b": ""}] * 30)
        result = run(model, {})
        self.assertGreaterEqual(result.savings.keystrokes_by_hand,
                                ASSUMED_LENGTH * len(model.targets()))


class TestSweep(unittest.TestCase):
    def test_many_cases_total_up_to_the_sum_of_their_runs(self):
        model = tiny_model()
        cases = tiny_records(9)
        total = sweep(model, cases, ["city"])
        one_by_one = [run(model, {"city": c["city"]}, case=c) for c in cases]
        self.assertEqual(total.cases, 9)
        self.assertEqual(total.filled, sum(r.savings.filled for r in one_by_one))
        self.assertAlmostEqual(
            total.seconds_by_hand, sum(r.savings.seconds_by_hand for r in one_by_one))
        self.assertEqual(total.right, sum(r.score.right for r in one_by_one))

    def test_a_seed_field_the_model_has_never_heard_of_is_ignored(self):
        model = tiny_model()
        total = sweep(model, tiny_records(4), ["city", "not_a_field"])
        self.assertEqual(total.seeds, ["city"])

    def test_no_cases_is_reported_rather_than_divided_by(self):
        total = sweep(tiny_model(), [], ["city"])
        self.assertEqual(total.cases, 0)
        self.assertEqual(total.accuracy, 0.0)
        self.assertEqual(total.share_saved, 0.0)
        self.assertEqual(total.per_case(10), 0.0)

    def test_a_model_that_costs_more_than_it_saves_says_so_plainly(self):
        # Filling boxes wrongly is worse than leaving them empty, and
        # "-3% less" makes the reader do the noticing.
        from fillerai.simulate.run import _share_words
        self.assertEqual(_share_words(0.25), "25% less")
        self.assertEqual(_share_words(-0.03), "3% more")

    def test_the_headline_says_the_accuracy_and_the_saving_together(self):
        # Either number on its own can flatter: filling everything badly, or
        # filling one field perfectly.
        line = sweep(tiny_model(), tiny_records(6), ["city"]).headline()
        self.assertIn("accuracy", line)
        self.assertIn("less", line)


class TestStylesheet(unittest.TestCase):
    """One structural check on the stylesheet, for a bug tests cannot see.

    The simulate panel first shipped with a ``.mark`` class on its right and
    wrong notes. ``.mark`` was already the brand square in the topbar, which
    is a solid accent-blue block, so every verdict on the panel came out as
    white text on a blue tile. Nothing failed, nothing logged, and the only
    way to notice was to look. A bare class name defined twice is the shape
    of that bug, so that shape is what this looks for.
    """

    def test_no_bare_class_is_defined_twice_in_the_stylesheet(self):
        import re

        sheet = (Path(__file__).resolve().parent.parent / "fillerai" / "web"
                 / "static" / "styles.css").read_text(encoding="utf-8")
        # Only selectors that are one plain class on their own. A compound
        # (".controls.tight") or a descendant (".card .sub") is a deliberate
        # modifier of something, not an accidental second owner of a name.
        seen: dict[str, int] = {}
        for line in sheet.splitlines():
            match = re.match(r"^\.([a-zA-Z][\w-]*)\s*\{", line)
            if match:
                seen[match.group(1)] = seen.get(match.group(1), 0) + 1
        twice = sorted(name for name, count in seen.items() if count > 1)
        self.assertEqual(twice, [], f"defined more than once: {twice}")


class TestSimulateCli(unittest.TestCase):
    def test_the_command_runs_the_same_engine_the_ui_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "m.json"
            model_path.write_text(tiny_model().to_json(), encoding="utf-8")
            out = Path(tmp) / "report.json"
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = main(["simulate", str(model_path), "-n", "5",
                             "--seed", "3", "--seeds", "city", "--show",
                             "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("cases", out.read_text(encoding="utf-8"))
            # --show prints the form, and the assumptions are always stated
            # alongside any number built on them.
            self.assertIn("counted as:", printed.getvalue())
            self.assertIn("one form in full", printed.getvalue())

    def test_a_seed_field_that_does_not_exist_is_named_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "m.json"
            model_path.write_text(tiny_model().to_json(), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["simulate", str(model_path), "--seeds", "nope"]), 1)


if __name__ == "__main__":
    unittest.main()
