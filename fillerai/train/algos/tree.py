"""Decision trees, and a forest of them.

A form's fields are not independent one at a time, and that is the whole
argument for this engine. The conditional-table engine asks each field
separately what it implies and averages the answers, so a relationship that
only exists in a *combination* is invisible to it: "policy type is family
**and** the claimant is a dependent, therefore relationship is child" moves
neither field's own table much, and averaging two weak opinions keeps them
weak. A tree splits on one field and then asks the next question inside that
branch, which is exactly how a conditional relationship is written down.

Everything here is plain Python. There is no scikit-learn in this project
and there is not going to be: the point of FillerAI is that it runs inside
the locked-down environment where the real form data already lives, and
nothing installs there.

Three things about growing a tree on form data are not the textbook default,
and each is a correction for something that goes visibly wrong without it.

**Gain ratio, not information gain.** A field with a distinct value in every
record splits the rows into buckets of one, every bucket is pure, and plain
information gain calls that a perfect split. It is the same memorising
failure :mod:`..associate` guards against with leave-one-out, in a different
costume. Quinlan's gain ratio divides the gain by the entropy of the split
itself, so a 300-way split has to earn its width. A claim number then scores
near zero, which is the truth.

**Branches are capped and rare values pooled.** Past a dozen or so children a
split is a lookup table, not a question. The commonest values keep their own
branch and everything else shares an "anything else" branch, which also gives
the tree somewhere to send a value it has never seen.

**A missing feature descends every branch at once.** This is the one that
matters most here, and it is not an optimisation. A classifier normally has
all its features; this one is asked to finish a form from whichever three
boxes an agent happened to type first, so at prediction time most of the tree
is asking about fields nobody has filled in. Stopping at the first unknown
question - the obvious thing - would make the root split the only one that
ever fires. Instead, an unknown question is answered by every branch in
proportion to how many training rows went down it, which is Quinlan's
fractional-instance rule. A deeper question the agent *has* answered still
gets asked, so the tree uses whatever it is given and nothing it is not.

The forest exists because one tree can only ask its own root question first.
Grow ten trees on bootstrap samples, each offered a random handful of the
fields, and whatever the agent has typed, some tree in the stand roots on it.
That is bagging doing its usual job - lower variance, better on unseen rows -
plus one specific to this problem: coverage of the fields a user might type.
Its cost is the tree you can read, which is why both are offered.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..features import Profile
from ..trace import Trace
from .base import Algorithm, Ballot, Engine, FitContext, Guess, register, tuning

# Past this many children a split has stopped being a question. The rest of
# the values share the "anything else" branch, which doubles as the landing
# place for a value the tree never saw.
MAX_BRANCHES = 12
OTHER = "\x00other"

# A node keeps the head of its target distribution. The tail never wins a
# prediction and dropping it is what keeps a forest over a 45-field form a
# JSON file a person can open.
KEEP_PER_NODE = 6

# A candidate holding less than this share of a node never wins a prediction,
# and dropping it is most of the difference between a forest you can open in
# an editor and one you cannot.
MIN_NODE_SHARE = 0.02

# Below these a split is fitting noise. Deliberately blunt numbers: a form
# dataset is hundreds of rows, not millions, and a leaf of two is an anecdote.
MIN_ROWS_SPLIT = 12
MIN_ROWS_LEAF = 4

# A split has to actually buy something. Gain ratio is already a strict
# measure, so this only sweeps up the splits that are pure noise.
MIN_GAIN_RATIO = 0.01


# ----------------------------------------------------------------------
# the tree itself
# ----------------------------------------------------------------------


@dataclass
class Node:
    """One question, or one answer.

    Every node carries its own distribution, not just the leaves. That is
    what makes the missing-feature rule above cheap: any node can be asked
    what it thinks without descending further.
    """

    rows: int
    distribution: dict[str, float]
    feature: str | None = None
    children: dict[str, "Node"] = dc_field(default_factory=dict)

    @property
    def leaf(self) -> bool:
        return self.feature is None or not self.children

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rows": self.rows,
            "p": [[v, round(p, 4)] for v, p in self.distribution.items()],
        }
        if not self.leaf:
            out["on"] = self.feature
            out["kids"] = {value: child.to_dict() for value, child in self.children.items()}
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Node":
        return cls(
            rows=int(data.get("rows", 0)),
            distribution={str(v): float(p) for v, p in (data.get("p") or [])},
            feature=data.get("on"),
            children={
                str(value): cls.from_dict(child)
                for value, child in (data.get("kids") or {}).items()
            },
        )

    def depth(self) -> int:
        return 1 + max((child.depth() for child in self.children.values()), default=0)

    def size(self) -> int:
        return 1 + sum(child.size() for child in self.children.values())


@dataclass
class Tree:
    """A grown tree for one target field, with what it learned about itself."""

    root: Node
    importance: dict[str, float] = dc_field(default_factory=dict)
    strength: float = 0.0  # errors removed over always guessing the commonest
    accuracy: float = 0.0  # on the rows it was not grown on
    tested: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "importance": {k: round(v, 4) for k, v in self.importance.items()},
            "strength": round(self.strength, 4),
            "accuracy": round(self.accuracy, 4),
            "tested": self.tested,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tree":
        return cls(
            root=Node.from_dict(data["root"]),
            importance={str(k): float(v) for k, v in (data.get("importance") or {}).items()},
            strength=float(data.get("strength", 0.0)),
            accuracy=float(data.get("accuracy", 0.0)),
            tested=int(data.get("tested", 0)),
        )


# ----------------------------------------------------------------------
# growing
# ----------------------------------------------------------------------


def _entropy(counts: dict[str, int], total: int) -> float:
    if total <= 0:
        return 0.0
    out = 0.0
    for count in counts.values():
        if count:
            share = count / total
            out -= share * math.log2(share)
    return out


def _distribution(values: list[str], rows: list[int]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for row in rows:
        value = values[row]
        counts[value] = counts.get(value, 0) + 1
    total = len(rows)
    if not total:
        return {}
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:KEEP_PER_NODE]
    # Normalised over every row in the node, not over the kept head, so a
    # node whose tail is most of its mass comes out appropriately unsure
    # rather than pretending the head is the whole story.
    kept = {value: count / total for value, count in ordered
            if count / total >= MIN_NODE_SHARE}
    if not kept:  # every value is rare; the commonest is still the answer
        value, count = ordered[0]
        kept = {value: count / total}
    return kept


def _branches(feature_values: list[str], rows: list[int]) -> dict[str, list[int]]:
    """Split ``rows`` by one feature, pooling rare values into one branch.

    Rows where the feature is blank take no part in the split: a blank on a
    form means "not applicable here", which is a different fact from any of
    the values, and giving it its own branch invites the tree to learn which
    fields the generator left empty.
    """
    groups: dict[str, list[int]] = {}
    for row in rows:
        value = feature_values[row]
        if not value:
            continue
        groups.setdefault(value, []).append(row)
    if len(groups) <= MAX_BRANCHES:
        return groups

    order = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    kept = dict(order[:MAX_BRANCHES - 1])
    other: list[int] = []
    for _value, members in order[MAX_BRANCHES - 1:]:
        other.extend(members)
    if other:
        kept[OTHER] = sorted(other)
    return kept


def _gain_ratio(target_values: list[str], rows: list[int],
                groups: dict[str, list[int]]) -> float:
    """Information gain divided by the entropy of the split itself.

    The division is the whole point. Gain alone rewards a split for being
    wide, so a field with a different value in every record wins every time
    while having taught the tree nothing it can reuse.
    """
    total = len(rows)
    split_rows = sum(len(members) for members in groups.values())
    if total <= 0 or split_rows <= 0 or len(groups) < 2:
        return 0.0

    counts: dict[str, int] = {}
    for row in rows:
        value = target_values[row]
        counts[value] = counts.get(value, 0) + 1
    before = _entropy(counts, total)

    after = 0.0
    split_info = 0.0
    for members in groups.values():
        share = len(members) / split_rows
        child: dict[str, int] = {}
        for row in members:
            value = target_values[row]
            child[value] = child.get(value, 0) + 1
        after += share * _entropy(child, len(members))
        split_info -= share * math.log2(share)

    # Rows the split could not place (the feature was blank for them) keep
    # their share of the original uncertainty rather than vanishing.
    covered = split_rows / total
    gain = before - (covered * after + (1 - covered) * before)
    if split_info <= 0:
        return 0.0
    return max(0.0, gain / split_info)


def _grow(target_values: list[str], columns: dict[str, list[str]], features: list[str],
          rows: list[int], depth: int, max_depth: int,
          importance: dict[str, float]) -> Node:
    node = Node(rows=len(rows), distribution=_distribution(target_values, rows))
    if depth >= max_depth or len(rows) < MIN_ROWS_SPLIT:
        return node
    # A pure node has nothing left to ask about.
    if len(node.distribution) <= 1:
        return node

    best_feature, best_groups, best_score = None, None, MIN_GAIN_RATIO
    for feature in features:
        groups = _branches(columns[feature], rows)
        if len(groups) < 2 or any(len(m) < MIN_ROWS_LEAF for m in groups.values()):
            # Dropping the whole split rather than the thin branch: a split
            # whose branches are anecdotes is an anecdote.
            groups = {value: members for value, members in groups.items()
                      if len(members) >= MIN_ROWS_LEAF}
            if len(groups) < 2:
                continue
        score = _gain_ratio(target_values, rows, groups)
        # Ties broken by name so a tree grown twice on the same data is the
        # same tree - a model you cannot diff is a model you cannot review.
        if score > best_score or (score == best_score and best_feature
                                  and feature < best_feature):
            best_feature, best_groups, best_score = feature, groups, score

    if best_feature is None or best_groups is None:
        return node

    importance[best_feature] = importance.get(best_feature, 0.0) + best_score * len(rows)
    remaining = [f for f in features if f != best_feature]
    node.feature = best_feature
    node.children = {
        value: _grow(target_values, columns, remaining, sorted(members),
                     depth + 1, max_depth, importance)
        for value, members in sorted(best_groups.items())
    }
    return node


# ----------------------------------------------------------------------
# asking
# ----------------------------------------------------------------------


def _descend(node: Node, known: dict[str, str],
             path: list[str]) -> dict[str, float]:
    """What this node thinks, following whichever questions can be answered.

    The three cases are the whole missing-value policy: the question is
    answered and the branch exists, the question is answered with something
    the tree never saw, or the question cannot be answered at all. Only the
    last averages over branches.
    """
    if node.leaf:
        return node.distribution

    value = known.get(node.feature or "", "")
    if value:
        child = node.children.get(value) or node.children.get(OTHER)
        if child is None:
            # A value the tree never saw, and no "anything else" branch to
            # send it down. This node's own distribution is the honest answer.
            return node.distribution
        shown = value if value in node.children else "anything else"
        path.append(f"{node.feature} = {shown}")
        return _descend(child, known, path)

    # Unanswered: every branch speaks, in proportion to the rows behind it.
    total = sum(child.rows for child in node.children.values())
    if total <= 0:
        return node.distribution
    mixed: dict[str, float] = {}
    for child in node.children.values():
        weight = child.rows / total
        for candidate, probability in _descend(child, known, path).items():
            mixed[candidate] = mixed.get(candidate, 0.0) + weight * probability
    return mixed


# ----------------------------------------------------------------------
# the engine
# ----------------------------------------------------------------------


class TreeEngine(Engine):
    """One or more trees per field, asked together."""

    algorithm = "tree"

    def __init__(self, trees: dict[str, list[Tree]], label: str = "tree") -> None:
        self.trees = trees
        self.label = label

    # -- prediction --------------------------------------------------------

    def guess(self, name: str, known: dict[str, str], profile: Profile) -> Guess:
        ballot = Ballot()
        stand = self.trees.get(name) or []
        for index, tree in enumerate(stand):
            path: list[str] = []
            distribution = _descend(tree.root, known, path)
            if not distribution:
                continue
            # A tree that answered without being able to ask anything has
            # just recited the marginal, so it is not evidence.
            weight = tree.strength if path else tree.strength * 0.25
            if weight <= 0:
                continue
            ballot.cast(distribution, weight, evidence=bool(path))
            if path and len(ballot.because) < 3:
                top = max(distribution, key=lambda c: distribution[c])
                where = " then ".join(path)
                lead = "tree" if len(stand) == 1 else f"tree {index + 1}"
                ballot.because.append(
                    f"{lead}: {where} -> {top} in {distribution[top] * 100:.0f}% of "
                    f"those records"
                )
        ballot.floor(profile)
        return ballot.guess()

    # -- description -------------------------------------------------------

    def targets(self) -> list[str]:
        return list(self.trees)

    def _importance(self, name: str) -> dict[str, float]:
        total: dict[str, float] = {}
        for tree in self.trees.get(name) or []:
            for feature, weight in tree.importance.items():
                total[feature] = total.get(feature, 0.0) + weight
        return total

    def sources(self, name: str) -> list[str]:
        importance = self._importance(name)
        return [f for f, _ in sorted(importance.items(), key=lambda kv: (-kv[1], kv[0]))]

    def strength(self, name: str) -> float:
        stand = self.trees.get(name) or []
        if not stand:
            return 0.0
        return max(tree.strength for tree in stand)

    def reach(self, name: str, available: set[str]) -> float:
        """Scaled by how much of what the trees ask about has been typed.

        A tree that asks three questions and has been told one answer is not
        at full strength and is not at nothing either. Scoring it in
        proportion to the importance covered is what lets the greedy seed
        search climb towards a pair of fields that only work together - the
        same reason the rule scoring in :mod:`..evaluate` gives partial
        credit.
        """
        importance = self._importance(name)
        total = sum(importance.values())
        if total <= 0:
            return 0.0
        covered = sum(weight for feature, weight in importance.items()
                      if feature in available)
        return self.strength(name) * (covered / total)

    def explain(self, name: str) -> str:
        sources = self.sources(name)
        if not sources:
            return ""
        stand = self.trees.get(name) or []
        depth = max((tree.root.depth() for tree in stand), default=1) - 1
        asked = ", ".join(sources[:3])
        return f"asks about {asked}" + (f", {depth} deep" if depth > 1 else "")

    def summary(self) -> dict[str, Any]:
        stand = [tree for group in self.trees.values() for tree in group]
        if not stand:
            return {"trees": 0}
        return {
            "trees": len(stand),
            "fields with a tree": len(self.trees),
            "deepest": max(tree.root.depth() for tree in stand) - 1,
            "nodes": sum(tree.root.size() for tree in stand),
            "best field": round(max(tree.strength for tree in stand), 3),
        }

    def drawn(self, name: str, max_lines: int = 60) -> list[str]:
        """The first tree for ``name``, as indented text.

        The readable tree is half the reason this algorithm is offered, so
        there is a way to actually read it.
        """
        stand = self.trees.get(name) or []
        if not stand:
            return []
        lines: list[str] = []

        def walk(node: Node, indent: str, label: str | None) -> None:
            if len(lines) >= max_lines:
                return
            top = sorted(node.distribution.items(), key=lambda kv: (-kv[1], kv[0]))[:1]
            says = f"{top[0][0]} ({top[0][1] * 100:.0f}%)" if top else "-"
            head = f"{indent}{label + ': ' if label else ''}"
            if node.leaf:
                lines.append(f"{head}{says}  [{node.rows} records]")
                return
            lines.append(f"{head}{node.feature}?  [{node.rows} records, says {says}]")
            for value, child in node.children.items():
                shown = "anything else" if value == OTHER else value
                walk(child, indent + "    ", shown)

        walk(stand[0].root, "", None)
        if len(lines) >= max_lines:
            lines.append("...")
        return lines

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "label": self.label,
            "trees": {name: [tree.to_dict() for tree in stand]
                      for name, stand in self.trees.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TreeEngine":
        engine = cls(
            {name: [Tree.from_dict(t) for t in stand]
             for name, stand in (data.get("trees") or {}).items()},
            label=str(data.get("label", "tree")),
        )
        engine.algorithm = str(data.get("algorithm", "tree"))
        return engine


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------


def _score(tree_root: Node, target_values: list[str], columns: dict[str, list[str]],
           features: list[str], rows: list[int]) -> tuple[float, float, int]:
    """Accuracy on rows the tree was not grown on, and lambda beside it.

    Accuracy on its own flatters a tree on a form where 94% of records say
    the same thing. What is reported as strength is the share of guessing
    errors the tree removes over always saying the commonest value, which is
    the same quantity :mod:`..associate` reports as lambda - so the number
    beside a tree and the number beside a conditional table mean the same
    thing and the field report can mix them.
    """
    if not rows:
        return 0.0, 0.0, 0

    counts: dict[str, int] = {}
    for row in rows:
        value = target_values[row]
        counts[value] = counts.get(value, 0) + 1
    majority = max(counts.values()) / len(rows)

    hits = 0
    for row in rows:
        known = {f: columns[f][row] for f in features if columns[f][row]}
        distribution = _descend(tree_root, known, [])
        if not distribution:
            continue
        best = max(distribution, key=lambda c: (distribution[c], c))
        if best == target_values[row]:
            hits += 1
    accuracy = hits / len(rows)
    strength = 0.0 if majority >= 1.0 else max(0.0, (accuracy - majority) / (1 - majority))
    return accuracy, strength, len(rows)


def _filled_rows(values: list[str]) -> list[int]:
    return [index for index, value in enumerate(values) if value]


def _fit(context: FitContext, trees_per_field: int, max_depth: int,
         features_per_tree: int | None, label: str) -> TreeEngine:
    """Grow ``trees_per_field`` trees for every field worth predicting."""
    trace: Trace = context.trace
    columns = context.columns
    usable = context.usable()
    seed = getattr(context.options, "seed", None)
    rng = random.Random(0 if seed is None else seed)

    trace.step(
        f"growing {trees_per_field} tree(s) per field over {len(usable)} fields, "
        f"at most {max_depth} question(s) deep"
    )

    trees: dict[str, list[Tree]] = {}
    for position, target in enumerate(usable, start=1):
        target_values = columns[target]
        rows = _filled_rows(target_values)
        if len(rows) < MIN_ROWS_SPLIT:
            trace.detail(f"  {target}: only {len(rows)} filled record(s), left to the marginal")
            continue
        candidates = [name for name in usable if name != target]
        if not candidates:
            continue

        stand: list[Tree] = []
        for index in range(trees_per_field):
            if trees_per_field == 1:
                # One tree gets all the fields and a clean split: grown on
                # most of the rows, scored on the rest.
                offered = list(candidates)
                shuffled = list(rows)
                rng.shuffle(shuffled)
                cut = max(1, len(shuffled) // 5)
                held, grown = shuffled[:cut], shuffled[cut:]
            else:
                # Bagging: a bootstrap sample to grow on, the rows it missed
                # to score on, and a random handful of the fields so the
                # stand between them roots on many different questions.
                take = features_per_tree or max(3, int(len(candidates) ** 0.5) + 1)
                offered = rng.sample(candidates, min(take, len(candidates)))
                grown = [rng.choice(rows) for _ in rows]
                held = sorted(set(rows) - set(grown))
                if not held:
                    held = rows[: max(1, len(rows) // 5)]

            importance: dict[str, float] = {}
            root = _grow(target_values, columns, offered, sorted(grown), 0,
                         max_depth, importance)
            accuracy, strength, tested = _score(root, target_values, columns,
                                                offered, held)
            total = sum(importance.values()) or 1.0
            stand.append(Tree(
                root=root,
                importance={k: v / total for k, v in importance.items()},
                strength=strength, accuracy=accuracy, tested=tested,
            ))

        # A stand of trees that learned nothing is worse than no stand: it
        # would vote the marginal in at full weight and drown the floor.
        if any(tree.strength > 0 for tree in stand):
            trees[target] = stand
        if position % 5 == 0 or position == len(usable):
            trace.detail(f"  grown for {position}/{len(usable)} fields")

    best = sorted(
        ((name, max(t.strength for t in stand)) for name, stand in trees.items()),
        key=lambda kv: -kv[1],
    )
    trace.done(f"{len(trees)} field(s) got a usable {label}")
    for name, strength in best[:8]:
        # Summed across the stand first: taking the top two out of every
        # tree's own list names the same field twice as often as not.
        shared: dict[str, float] = {}
        for grown in trees[name]:
            for feature, weight in grown.importance.items():
                shared[feature] = shared.get(feature, 0.0) + weight
        asked = ", ".join(
            f for f, _ in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        )
        trace.detail(f"  {name}: removes {strength * 100:.0f}% of the guessing, "
                     f"asking about {asked or 'nothing'}")
    return TreeEngine(trees, label=label)


def fit_tree(context: FitContext) -> TreeEngine:
    depth = tuning(context, "max_depth", 4)
    engine = _fit(context, trees_per_field=1, max_depth=depth,
                  features_per_tree=None, label="tree")
    engine.algorithm = "tree"
    return engine


def fit_forest(context: FitContext) -> TreeEngine:
    depth = tuning(context, "max_depth", 5)
    count = max(2, min(40, tuning(context, "trees", 10)))
    engine = _fit(context, trees_per_field=count, max_depth=depth,
                  features_per_tree=tuning(context, "features_per_tree", 0) or None,
                  label="forest")
    engine.algorithm = "forest"
    return engine


def _load(data: dict[str, Any]) -> TreeEngine:
    return TreeEngine.from_dict(data)


ALGORITHM_TREE = register(Algorithm(
    name="tree",
    label="Decision tree",
    blurb="One tree per field, asking about the other fields in the order that "
          "separates the answers fastest. The tree can be read line by line, "
          "which makes this the one to reach for when someone has to be "
          "convinced the model is sensible.",
    recipe=[
        "For each field worth predicting, take the records where it is filled.",
        "Hold a fifth of them back, unseen, to score the finished tree on.",
        "Try splitting on every other field and score each split by gain ratio: "
        "how much uncertainty it removes, divided by how wide it is.",
        "Cap the branches at a dozen; rarer values share an 'anything else' branch.",
        "Take the best split, and repeat inside each branch until the tree is "
        "deep enough, the rows run thin, or nothing is left to ask.",
        "Score the tree on the held-back rows and record how much of the guessing "
        "it removes over always saying the commonest value.",
        "To predict, walk the tree answering what the agent has typed; an "
        "unanswered question is averaged over its branches by their size.",
    ],
    fit=fit_tree,
    load=_load,
    knobs=[("max_depth", "Questions deep", "int", 4, 1, 8)],
))


ALGORITHM_FOREST = register(Algorithm(
    name="forest",
    label="Random forest",
    blurb="Ten trees per field, each grown on a resample of the records and "
          "offered a random handful of the fields, then averaged. Usually the "
          "most accurate choice here, and the one least bothered by which "
          "fields the agent happens to type first.",
    recipe=[
        "For each field worth predicting, grow several trees instead of one.",
        "Give each tree a bootstrap resample of the records, so they disagree.",
        "Offer each tree only a random handful of the other fields, so between "
        "them the stand roots on many different questions.",
        "Grow each tree exactly as the single tree is grown, by gain ratio.",
        "Score each tree on the records its resample happened to miss.",
        "To predict, let every tree walk down and average their answers, each "
        "weighted by how much guessing that tree was measured to remove.",
    ],
    fit=fit_forest,
    load=_load,
    knobs=[
        ("trees", "Trees per field", "int", 10, 2, 40),
        ("max_depth", "Questions deep", "int", 5, 1, 8),
    ],
))
