"""A cheap answer to "which fields is this field worth asking about?".

Two of the engines here need a shortlist of relevant fields per target and
neither needs it to be the careful one. :mod:`..associate` gives the careful
one - leave-one-out, against a shuffled baseline, three trials of it - and
that care costs most of a fit. A nearest-neighbour search picking which
fields to weight, or a naive Bayes picking which fields to multiply, is
making a coarser decision and can afford a coarser measure.

So this is one pass of Quinlan's gain ratio per ordered pair: how much
knowing the source narrows the target, divided by how wide the source's split
is. The division is what stops a field with a distinct value per record
topping every shortlist, which is the same failure lambda's leave-one-out
guards against and the reason plain information gain is not used.

It is a shortlist, not a score anyone is shown. Where a number reaches a
report or a confidence, it has been measured on held-out records.
"""

from __future__ import annotations

from .tree import _gain_ratio

# Enough rows to rank fields by, and few enough that this stays the cheap
# step. The ranking is stable well before every record is read.
SAMPLE_ROWS = 400

# Below this a source has nothing to say about the target.
MIN_RELEVANCE = 0.01


def table(columns: dict[str, list[str]], usable: list[str],
          keep: int = 8, sources: list[str] | None = None,
          source_columns: dict[str, list[str]] | None = None
          ) -> dict[str, dict[str, float]]:
    """``{target: {source: relevance}}``, best sources only.

    ``sources`` and ``source_columns`` let a caller offer evidence the targets
    are not drawn from, and offer it in a different form. :mod:`.linear` uses
    both: it can predict from a field with thousands of distinct values by
    bucketing them, so it asks about more fields than it answers, and asks
    about them as buckets rather than as values. The measure is the same one
    either way.
    """
    rows = len(next(iter(columns.values()), []))
    step = max(1, rows // SAMPLE_ROWS)
    sample = list(range(0, rows, step))
    offered = usable if sources is None else sources
    from_columns = columns if source_columns is None else source_columns

    out: dict[str, dict[str, float]] = {}
    for target in usable:
        target_values = columns[target]
        filled = [row for row in sample if target_values[row]]
        if not filled:
            continue
        scores: dict[str, float] = {}
        for source in offered:
            if source == target:
                continue
            groups: dict[str, list[int]] = {}
            for row in filled:
                value = from_columns[source][row]
                if value:
                    groups.setdefault(value, []).append(row)
            if len(groups) < 2:
                continue
            score = _gain_ratio(target_values, filled, groups)
            if score >= MIN_RELEVANCE:
                scores[source] = score
        if scores:
            best = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:keep]
            out[target] = dict(best)
    return out
