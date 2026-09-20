"""The selectable training algorithms.

Importing this package registers every algorithm, which is what makes
:func:`get` and :func:`all_algorithms` know about them. They are listed here
in the order a picker should show them: the one that is hardest to misread
first, the one that is usually most accurate second, and the two that are
here because they are the right shape for a form - a tree you can read, and
a search over past records - after that.

Adding another means one module, one :class:`Algorithm` registered at the
bottom of it, and one line here. Nothing above this package changes: the
shared layer profiles the columns, verifies the rules, holds records back,
calibrates and scores whatever engine comes out, which is what makes two
algorithms comparable rather than merely both available.
"""

from __future__ import annotations

from .base import (
    Algorithm,
    Ballot,
    Engine,
    FitContext,
    Guess,
    all_algorithms,
    get,
    names,
    register,
)
from . import statistical, tree, nearest, bayes  # noqa: F401 - registers them

#: What a caller gets without saying. The conditional-table engine, because
#: it is the one whose every number can be checked by hand, and because it is
#: what every model saved before this choice existed was fitted with.
DEFAULT = "statistical"

__all__ = [
    "Algorithm",
    "Ballot",
    "DEFAULT",
    "Engine",
    "FitContext",
    "Guess",
    "all_algorithms",
    "get",
    "names",
    "register",
]
