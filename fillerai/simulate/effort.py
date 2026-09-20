"""What filling a form costs, so what autofilling it saves can be stated.

The saving is the whole argument for this project, and an argument needs a
number. That number cannot be measured from here - nobody is being timed -
so it is modelled, and the model is written down rather than buried: an
agent finds a box, types into it or picks from it, and moves on. Every
constant below is an assumption, visible and adjustable, and the reports
built on them say which assumptions produced them.

Two costs are counted per field, because they answer different questions.
Keystrokes are exact and dull: a claim number is fourteen characters
whoever types it. Seconds are an estimate and the thing anyone actually
cares about, and most of a dense form's time is not spent typing at all -
it goes on moving between forty boxes, which is why a per-field overhead
sits beside the typing rate rather than being folded into it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Controls you choose from rather than type into. Their cost has nothing to
# do with the length of the value: picking "Massachusetts" from a dropdown
# is the same work as picking "Ohio".
PICKED_CONTROLS = frozenset({
    "select", "multiselect", "radio", "checkbox", "date", "datetime-local",
    "time", "month", "week", "color", "range",
})


@dataclass(frozen=True)
class Effort:
    """The assumptions behind every saving this package reports."""

    # Sustained rate on form data - identifiers, codes, postal codes - which
    # is well below anyone's prose speed because none of it is muscle memory.
    chars_per_second: float = 5.0
    # Finding the next box and focusing it, before a character is typed. On a
    # form of forty fields this, not the typing, is the bulk of the time.
    seconds_per_field: float = 1.2
    # Opening a picker and choosing from it.
    seconds_to_pick: float = 2.0
    # Reading a value that is already filled in and accepting it. Autofill is
    # not free: the agent is still responsible for what they submit.
    seconds_to_review: float = 0.6
    # Noticing that a filled value is wrong, on top of then typing it out.
    # A wrong autofill costs more than an empty box, which is the reason the
    # model declines a field it cannot predict.
    seconds_to_notice: float = 1.0

    # -- per field ---------------------------------------------------------

    def type_cost(self, control: str, length: int) -> tuple[int, float]:
        """Keystrokes and seconds to fill one field from nothing."""
        if control in PICKED_CONTROLS:
            return 1, self.seconds_per_field + self.seconds_to_pick
        length = max(0, int(length))
        return length, self.seconds_per_field + length / self.chars_per_second

    def review_cost(self) -> tuple[int, float]:
        """Keystrokes and seconds to read a filled value and accept it."""
        return 0, self.seconds_to_review

    def correct_cost(self, control: str, length: int) -> tuple[int, float]:
        """Keystrokes and seconds to spot a wrong filled value and retype it."""
        keystrokes, seconds = self.type_cost(control, length)
        return keystrokes, seconds + self.seconds_to_notice

    # -- wording -----------------------------------------------------------

    def assumptions(self) -> list[str]:
        """The constants above, in the words a report should print them."""
        return [
            f"typing at {self.chars_per_second:.0f} characters a second",
            f"{self.seconds_per_field:.1f}s to move to each field",
            f"{self.seconds_to_pick:.1f}s to choose from a dropdown",
            f"{self.seconds_to_review:.1f}s to read a filled value and accept it",
            f"{self.seconds_to_notice:.1f}s to notice a wrong one before retyping it",
        ]

    def to_dict(self) -> dict[str, float]:
        return {
            "chars_per_second": self.chars_per_second,
            "seconds_per_field": self.seconds_per_field,
            "seconds_to_pick": self.seconds_to_pick,
            "seconds_to_review": self.seconds_to_review,
            "seconds_to_notice": self.seconds_to_notice,
        }


DEFAULT_EFFORT = Effort()


def spell_out(seconds: float) -> str:
    """Seconds as a person would say them: 8s, 2m 40s, 1h 12m."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
