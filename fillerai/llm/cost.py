"""What a call is about to cost, said before it is made.

Every other stage of this project is free to run, so a stage that is not
should say so out loud rather than arriving on a bill later. Two things follow
from that and both are deliberate.

**An estimate is printed before the call, not after it.** ``--dry-run`` on any
command that would talk to a model prints the estimate and exits, which is the
cheapest way to find out that a form is larger than expected.

**Token counts here are approximations and are labelled as such.** Counting
them exactly means asking the API, which is a network call, which is the thing
being estimated. Four characters to a token is close enough to decide whether
something costs a penny or a pound, and it is wrong in the safe direction for
English prose with punctuation in it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Rough and honest. See the module docstring.
CHARS_PER_TOKEN = 4

#: US dollars per million tokens, (input, output). Published first-party
#: rates; a deployment behind ``FILLERAI_LLM_BASE_URL`` may well charge
#: something else, which is why :func:`estimate` says which model it priced.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: A cache read is a tenth of the input rate; writing the cache is a quarter
#: more than paying for the tokens outright. Only the read matters here,
#: because these features make one call at a time.
CACHE_READ_SHARE = 0.1
CACHE_WRITE_SHARE = 1.25


def tokens(text: str) -> int:
    """Roughly how many tokens a string is."""
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Estimate:
    """What one call is expected to cost, and on what assumptions."""

    model: str
    input_tokens: int
    output_tokens: int
    dollars: float
    priced: bool  # False when the model is not in PRICES

    def describe(self) -> list[str]:
        if not self.priced:
            return [
                f"about {self.input_tokens:,} tokens in and {self.output_tokens:,} out",
                f"no published price for {self.model} - cost unknown",
            ]
        return [
            f"about {self.input_tokens:,} tokens in and {self.output_tokens:,} out",
            f"roughly ${self.dollars:.2f} on {self.model}",
            "token counts estimated at 4 characters each",
        ]


def estimate(model: str, prompt: str, expected_output_tokens: int) -> Estimate:
    """Price one call, charging the whole prompt at the full input rate.

    No cache discount is applied. These features make a single call per run,
    so the prefix is written rather than read and the discount would be a
    fiction; over many runs the real figure drifts below this one, which is
    the direction an estimate should be wrong in.
    """
    in_tokens = tokens(prompt)
    rates = PRICES.get(model)
    if rates is None:
        return Estimate(model, in_tokens, expected_output_tokens, 0.0, False)
    dollars = (in_tokens * rates[0] + expected_output_tokens * rates[1]) / 1_000_000
    return Estimate(model, in_tokens, expected_output_tokens, dollars, True)


class SpendRefused(RuntimeError):
    """Raised when an estimate exceeds the ceiling the caller set."""


def enforce(est: Estimate, ceiling: float | None) -> None:
    """Refuse a run that would cost more than the caller allowed."""
    if ceiling is None or not est.priced:
        return
    if est.dollars > ceiling:
        raise SpendRefused(
            f"this run is estimated at ${est.dollars:.2f}, above the "
            f"${ceiling:.2f} ceiling; raise --max-spend to go ahead"
        )
