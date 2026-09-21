"""The gate that decides whether proposing rules is worth keeping.

Everything in ``tests/`` runs offline against a recorded exchange, which tests
the checks and says nothing at all about whether a model's proposals are any
good. That question costs money to answer, so it lives here, outside
``unittest discover -s tests``, and skips itself unless somebody has decided
to spend it::

    FILLERAI_LLM_LIVE=1 FILLERAI_LLM_KEY=sk-... python -m unittest discover -s livetests

The gate is the one written down in ``docs/llm-implementation-plan.md`` before
any of this was built, which is the point of writing it down first.

``claims_intake.html`` is markup, so it declares no rules at all and saves
**5.0%** of an agent's work. If asking a model for its rules does not move
that, and move it without making the generated data incoherent, then this
feature does not work and the honest thing is to delete it.

The baseline is measured in the same run rather than quoted, because a number
copied out of a README is a number nobody re-checks.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fillerai
from fillerai.generate.dataset import Options, coherence_report, generate, validate
from fillerai.simulate.run import sweep as simulate_forms
from fillerai.train.evaluate import suggest_seed_fields
from fillerai.train.model import train

LIVE = os.environ.get("FILLERAI_LLM_LIVE") == "1"
FORM = ROOT / "examples" / "claims_intake.html"

RECORDS = 600
CASES = 150
SEEDS = 3
#: What the form manages today with no declared rules. Measured, not quoted -
#: this is only here to fail loudly if the baseline itself has moved.
EXPECTED_BASELINE = 0.05


def saving(schema) -> tuple[float, list[str]]:
    """How much of the work this form's model saves, and what is wrong with the data."""
    options = Options(count=RECORDS + CASES, seed=11)
    records = [dict(r) for r in generate(schema, options).records]
    problems = validate(schema, records) + coherence_report(schema, records)
    model = train(schema, records[:RECORDS], seed=1)
    seeds = suggest_seed_fields(model, SEEDS)
    result = simulate_forms(model, records[RECORDS:], seeds)
    return result.to_dict()["share_saved"], problems


@unittest.skipUnless(LIVE, "set FILLERAI_LLM_LIVE=1 to spend money on this")
class TestProposedRulesAreWorthHaving(unittest.TestCase):
    """One call to a real model, and the measurement the plan promised."""

    @classmethod
    def setUpClass(cls):
        from fillerai.llm import rules as llm_rules

        cls.schema = fillerai.extract_html(FORM)
        cls.proposals = llm_rules.propose(cls.schema)
        cls.ruled = llm_rules._with_rules(cls.schema, cls.proposals.kept)

    def test_the_model_proposed_something_that_survived_the_checks(self):
        for line in self.proposals.report():
            print(line)
        self.assertGreater(
            len(self.proposals.kept), 0,
            "every proposed rule was dropped; the prompt or the gate is wrong",
        )

    def test_the_baseline_is_still_where_the_readme_says(self):
        baseline, problems = saving(self.schema)
        print(f"  baseline saving {baseline:.1%}, {len(problems)} problem(s)")
        self.assertAlmostEqual(baseline, EXPECTED_BASELINE, delta=0.03)

    def test_the_rules_do_not_make_the_generated_data_incoherent(self):
        """The gate's second half, and the one that is not negotiable."""
        _, before = saving(self.schema)
        _, after = saving(self.ruled)
        self.assertLessEqual(
            len(after), len(before),
            f"proposed rules introduced {len(after) - len(before)} new problem(s)",
        )

    def test_the_rules_raise_what_the_form_saves(self):
        """The gate itself. If this fails, do not ship phase 1."""
        before, _ = saving(self.schema)
        after, _ = saving(self.ruled)
        print(f"  {before:.1%} -> {after:.1%} with {len(self.proposals.kept)} rule(s)")
        self.assertGreater(
            after, before,
            "proposed rules did not raise the saving; phase 1 has not earned its place",
        )

    def test_the_proposals_can_be_written_out_and_read_back(self):
        written = json.dumps(self.proposals.to_dict(), indent=2)
        self.assertIn("kept", json.loads(written))


if __name__ == "__main__":
    unittest.main()
