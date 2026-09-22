"""Proposing the rules a form declares about itself - and disbelieving them.

Before 0.7.0 every dropdown on every generated form was ``rng.choice`` of its
own options, so business fields were uniform noise and the only structure a
model could find was what a persona brings with it. Declared rules fixed that,
and they moved the numbers further than any modelling change in this project's
history. But somebody has to write them: 27 by hand for the quote form, 31 for
onboarding.

This module asks a language model to propose them instead. The important half
is not the asking, it is everything after it.

**A proposed rule is a proposal.** The model is guessing - well, from a field
list and what it knows about the world, but guessing. So nothing it says is
believed until it has survived, in order:

1. this project's own spec parser, rather than a second one written here;
2. every name in ``follows`` being a real field, and not the target itself;
3. every value the rule can produce being one the target field can actually
   hold - which matters because ``_as_written`` deliberately does *not* fall
   back to ``choose_option``, so a value the form cannot take is a reported
   error and not silent noise;
4. the whole set being acyclic, checked here rather than left to
   ``_resolution_order``, which tolerates a cycle by leaving the fields where
   they were - correct for a generator that must produce a record anyway, and
   useless as an answer to "is this rule any good";
5. two hundred generated records through :func:`validate` and
   :func:`coherence_report`, compared against the same records generated
   without the new rules, so a rule that makes the data incoherent is dropped
   and its own error message says why.

**And nothing is written into a spec.** :func:`propose` returns proposals,
the command prints them for a person to read, and :func:`apply` is a separate
step. A model inventing plausible business logic is exactly what this is, and
the reviewer in the middle is the feature, not an obstacle to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..extract.spec import _derived_from_spec
from ..generate.dataset import Options, coherence_report, generate, validate
from ..schema import Derived, FormSchema
from . import cost, prompts
from .client import Client, Reply, ReplyError
from .config import Settings

#: Records generated to test the proposed rules against. Enough for a broken
#: rule to show up in every record it touches, small enough to be instant.
CHECK_SAMPLE = 200

#: Fields asked about in one call. Chosen so the answer fits the budget
#: :func:`prompts.rules_budget` allows with room left to think, and so a form
#: under this many option fields - which is every example in this repository
#: and most real forms - is still exactly one call.
BATCH_TARGETS = 60

#: How many times a group may be halved when an answer comes back truncated.
SPLIT_DEPTH = 2
CHECK_SEED = 20260921

#: Strips the record number off a problem so two runs can be compared by what
#: went wrong rather than by where.
_RECORD_PREFIX = re.compile(r"^record \d+: ")
#: Problem messages quote field names. That is how a new problem is traced
#: back to the rule that caused it.
_QUOTED = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'")


# --------------------------------------------------------------------------
# What comes back
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    """One rule a model suggested, before anyone has believed it."""

    field: str
    sources: tuple[str, ...]
    table: dict[str, tuple[str, ...]]  # keyed as Derived.key wants it
    otherwise: tuple[str, ...]
    confidence: float
    why: str
    #: The ``when`` block exactly as the model wrote it, so a spec ends up
    #: readable rather than normalised into lower case.
    raw_when: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_payload(cls, entry: dict[str, Any]) -> "Proposal":
        """Read one rule, through the spec parser the rest of the project uses.

        Going through :func:`_derived_from_spec` rather than reading the keys
        here is deliberate: a rule that this package would accept and
        ``spec.load`` would not is a bug waiting to be found by somebody else.
        """
        name = str(entry.get("field") or "").strip()
        if not name:
            raise ValueError("a rule arrived with no field name")
        derived = _derived_from_spec({
            "follows": entry.get("follows"),
            "when": entry.get("when") or {},
            "otherwise": entry.get("otherwise"),
        })
        if derived is None:
            raise ValueError(f"the rule for {name!r} names no field to follow")
        return cls(
            field=name,
            sources=derived.sources,
            table=derived.table,
            otherwise=derived.otherwise,
            confidence=float(entry.get("confidence") or 0.0),
            why=str(entry.get("why") or "").strip(),
            raw_when=dict(entry.get("when") or {}),
        )

    @property
    def derived(self) -> Derived:
        return Derived(sources=self.sources, table=self.table,
                       otherwise=self.otherwise)

    @property
    def values(self) -> set[str]:
        """Everything this rule can put in the field."""
        out: set[str] = set(self.otherwise)
        for produced in self.table.values():
            out.update(produced)
        return out

    def spec_entry(self) -> dict[str, Any]:
        """The rule as a field spec would carry it, in the readable spelling."""
        entry: dict[str, Any] = {
            "follows": self.sources[0] if len(self.sources) == 1 else list(self.sources),
            "when": self.raw_when,
        }
        if self.otherwise:
            entry["otherwise"] = (self.otherwise[0] if len(self.otherwise) == 1
                                  else list(self.otherwise))
        return entry

    def summary(self) -> str:
        return f"follows {', '.join(self.sources)}"

    def as_written(self, key: str) -> str:
        """A normalised table key, back in the spelling the model used.

        Keys are folded for lookup, so quoting one straight into an error
        message would report ``'sir'`` for a rule that actually said ``Sir``
        and send a reader looking for a typo that is this module's own.
        """
        for raw in self.raw_when:
            if Derived.key(str(raw).split("|")) == key:
                return str(raw)
        return key


@dataclass
class Verdict:
    """A proposal, and what the checks made of it."""

    proposal: Proposal
    problem: str | None = None

    @property
    def kept(self) -> bool:
        return self.problem is None


@dataclass
class Proposals:
    """Everything one run produced, checked."""

    schema_name: str
    verdicts: list[Verdict] = dc_field(default_factory=list)
    model: str = ""
    usage: dict[str, Any] = dc_field(default_factory=dict)
    estimate: cost.Estimate | None = None

    @property
    def kept(self) -> list[Proposal]:
        return [v.proposal for v in self.verdicts if v.kept]

    @property
    def dropped(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.kept]

    def to_dict(self) -> dict[str, Any]:
        return {
            "form": self.schema_name,
            "model": self.model,
            "kept": [
                {"field": p.field, **p.spec_entry(),
                 "confidence": round(p.confidence, 3), "why": p.why}
                for p in self.kept
            ],
            "dropped": [
                {"field": v.proposal.field, "why": v.proposal.why,
                 "problem": v.problem}
                for v in self.dropped
            ],
            "usage": dict(self.usage),
        }

    def report(self, *, review_below: float = 0.7) -> list[str]:
        """The run in the house style, ``?`` on anything worth a second look."""
        lines = [
            f"  == proposed rules for {self.schema_name}: "
            f"{len(self.kept)} kept, {len(self.dropped)} dropped =="
        ]
        for proposal in sorted(self.kept, key=lambda p: -p.confidence):
            marker = " " if proposal.confidence >= review_below else "?"
            lines.append(
                f"  {marker} {proposal.field:<28} {proposal.summary():<40} "
                f"{proposal.confidence:.2f}"
            )
            if proposal.why:
                lines.append(f"      {proposal.why}")
        if self.dropped:
            lines.append("    -- dropped --")
            for verdict in self.dropped:
                lines.append(f"    {verdict.proposal.field:<28} {verdict.problem}")
        low = [p for p in self.kept if p.confidence < review_below]
        if low:
            lines.append(
                f"\n  {len(low)} rule(s) marked '?' are worth a human look "
                "before they go in a spec."
            )
        return lines


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def _shapes(problems: list[str]) -> set[str]:
    return {_RECORD_PREFIX.sub("", problem) for problem in problems}


def with_rules(schema: FormSchema, proposals: list[Proposal]) -> FormSchema:
    """The same form, with these rules declared on it.

    A copy, because a caller's schema is not this module's to change. Public
    because the web UI applies rules to a schema rather than to a spec
    document, which is what :func:`apply` is for.
    """
    rules = {p.field: p.derived for p in proposals}
    clone = FormSchema.from_dict(schema.to_dict())
    for field in clone.fields:
        if field.name in rules:
            field.derived = rules[field.name]
    return clone


#: The old private name, kept because the tests and the live gate use it.
_with_rules = with_rules


def _structural(schema: FormSchema, proposal: Proposal) -> str | None:
    """What is wrong with this rule on its own, if anything."""
    by_name = {f.name: f for f in schema.fields}

    target = by_name.get(proposal.field)
    if target is None:
        return "no field of that name on this form"
    if target.derived is not None:
        return "the form already declares a rule for this field"
    if not proposal.sources:
        return "names no field to follow"
    if proposal.field in proposal.sources:
        return "follows itself"
    if len(set(proposal.sources)) != len(proposal.sources):
        return "names the same field twice"

    for source in proposal.sources:
        if source not in by_name:
            return f"follows {source!r}, which is not on this form"

    if not proposal.table and not proposal.otherwise:
        return "has no values in it"

    width = len(proposal.sources)
    for key in proposal.table:
        if len(key.split("|")) != width:
            return (f"keys on {key!r}, which is not {width} value(s) "
                    f"for {width} source field(s)")

    # A value the target cannot hold is the failure this check exists for.
    allowed = {o.value for o in target.options}
    if allowed:
        for value in sorted(proposal.values):
            if value not in allowed:
                return f"would set it to {value!r}, which is not one of its options"

    # And a key the source can never produce makes the rule dead rather than
    # wrong, which is worth saying differently.
    for index, source in enumerate(proposal.sources):
        source_values = {o.value.strip().lower() for o in by_name[source].options}
        if not source_values:
            continue
        for key in proposal.table:
            part = key.split("|")[index]
            if part and part not in source_values:
                shown = proposal.as_written(key).split("|")[index]
                return f"keys on {source}={shown!r}, which {source} cannot hold"
    return None


def _cyclic(schema: FormSchema, proposals: list[Proposal]) -> set[str]:
    """Fields caught in a cycle once these rules are added.

    ``_resolution_order`` tolerates a cycle by leaving the fields where they
    were, which is right for a generator that has to produce a record anyway
    and no use at all as a verdict on a rule. So the cycle is found here.
    """
    edges: dict[str, tuple[str, ...]] = {
        f.name: f.derived.sources for f in schema.fields if f.derived
    }
    edges.update({p.field: p.sources for p in proposals})

    caught: set[str] = set()
    done: set[str] = set()

    def walk(name: str, path: list[str]) -> None:
        if name in path:
            caught.update(path[path.index(name):])
            return
        if name in done:
            return
        path.append(name)
        for source in edges.get(name, ()):
            walk(source, path)
        path.pop()
        done.add(name)

    for name in list(edges):
        walk(name, [])
    return caught


def check(schema: FormSchema, proposals: list[Proposal], *,
          sample: int = CHECK_SAMPLE, seed: int = CHECK_SEED) -> list[Verdict]:
    """Put every proposal through the gauntlet, in the order that is cheapest.

    Structure first because it needs no data; then the graph, because one bad
    rule can only poison the set through a cycle; then the generator, which is
    the only check that can catch a rule that is well-formed and wrong.
    """
    verdicts = [Verdict(p, _structural(schema, p)) for p in proposals]

    # Two rules for one field is not a rule, it is a coin toss: the one
    # applied would be whichever happened to be last. The more confident one
    # stays and the rest say why they went, which matters now that a large
    # form is asked about in several calls and a model can answer outside the
    # slice it was given.
    seen: dict[str, Verdict] = {}
    for verdict in verdicts:
        if not verdict.kept:
            continue
        first = seen.get(verdict.proposal.field)
        if first is None:
            seen[verdict.proposal.field] = verdict
            continue
        loser = min((first, verdict), key=lambda v: v.proposal.confidence)
        winner = first if loser is verdict else verdict
        loser.problem = ("another proposal for this field was more confident "
                         f"({winner.proposal.confidence:.2f})")
        seen[verdict.proposal.field] = winner

    survivors = [v for v in verdicts if v.kept]
    caught = _cyclic(schema, [v.proposal for v in survivors])
    for verdict in survivors:
        if verdict.proposal.field in caught:
            verdict.problem = "would make a cycle with the other rules"

    survivors = [v for v in verdicts if v.kept]
    if not survivors:
        return verdicts

    options = Options(count=sample, seed=seed)
    baseline = _shapes(_problems(schema, options))

    for _ in range(2):  # attribute, drop, confirm - and no more than that
        live = [v for v in verdicts if v.kept]
        if not live:
            break
        ruled = _with_rules(schema, [v.proposal for v in live])
        new = _shapes(_problems(ruled, options)) - baseline
        if not new:
            return verdicts
        blamed = _blame(new, {v.proposal.field for v in live})
        for verdict in live:
            if verdict.proposal.field in blamed:
                verdict.problem = _first_problem(new, verdict.proposal.field)
        if not blamed:
            # Nothing named a field, so no rule can be singled out. Refusing
            # the whole set is the only honest answer left.
            for verdict in live:
                verdict.problem = "the rules together made the generated data incoherent"
            break
    return verdicts


def _problems(schema: FormSchema, options: Options) -> list[str]:
    dataset = generate(schema, options)
    records = [dict(r) for r in dataset.records]
    return validate(schema, records) + coherence_report(schema, records)


def _blame(problems: set[str], fields: set[str]) -> set[str]:
    named: set[str] = set()
    for problem in problems:
        named.update(set(_QUOTED.findall(problem)) & fields)
    return named


def _first_problem(problems: set[str], name: str) -> str:
    for problem in sorted(problems):
        if name in _QUOTED.findall(problem):
            return f"broke the generated data: {problem}"
    return "broke the generated data"


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------


def targets(schema: FormSchema) -> list[str]:
    """The fields a rule could decide, which is fewer than the form's fields.

    A rule puts a value in a field, and :func:`_structural` refuses any value
    the field cannot hold - so a field with no option list can never be the
    subject of one. Counting those fields in when sizing a run makes a
    seventy-field form look like a two-hundred-field one.
    """
    return [f.name for f in schema.fields if f.options and not f.derived]


def batches(schema: FormSchema, size: int = BATCH_TARGETS) -> list[list[str]]:
    """The form's targets, split into groups small enough to answer in one go.

    One group - the whole form - is the normal case and asks exactly the
    question this module has always asked. Several is the overflow path.
    """
    wanted = targets(schema)
    if size <= 0 or len(wanted) <= size:
        return [wanted]
    return [wanted[at:at + size] for at in range(0, len(wanted), size)]


def estimate(schema: FormSchema, settings: Settings,
             size: int = BATCH_TARGETS) -> cost.Estimate:
    """What this run would cost, before a socket is opened.

    A form too large for one call pays for the prompt again on each of them,
    so the estimate counts the calls rather than pricing the first one and
    letting the rest arrive on the bill.
    """
    groups = batches(schema, size)
    prompt = prompts.RULES_SYSTEM + prompts.rules_user(
        schema, groups[0] if len(groups) > 1 else None)
    # A form declares a rule for a minority of the fields that could carry
    # one; half is generous, and an estimate should be.
    expected = max(400, (len(targets(schema)) // 2) * prompts.TOKENS_PER_RULE)
    return cost.estimate(settings.model, prompt * len(groups), expected)


def parse(payload: Any) -> tuple[list[Proposal], list[str]]:
    """Read the model's answer, keeping what parses and reporting what does not."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise ValueError("the model's answer had no 'rules' list in it")
    proposals: list[Proposal] = []
    unreadable: list[str] = []
    for entry in payload["rules"]:
        if not isinstance(entry, dict):
            unreadable.append(f"a rule was {type(entry).__name__}, not an object")
            continue
        try:
            proposals.append(Proposal.from_payload(entry))
        except ValueError as error:
            unreadable.append(str(error))
    return proposals, unreadable


def _ask(client: Client, schema: FormSchema, group: list[str] | None,
         *, depth: int = 0) -> list[Reply]:
    """One call - or, if the answer came back cut off, two smaller ones.

    Truncation is the one failure worth retrying rather than reporting. The
    budget is sized from the form, but how much a model thinks before it
    answers is its business and not something this side can predict, so a
    large form can still overrun. Halving the question halves the answer, and
    two levels of that is enough for any form anybody has put through this.
    """
    try:
        return [client.ask(
            system=prompts.RULES_SYSTEM,
            user=prompts.rules_user(schema, group),
            output_schema=prompts.RULES_OUTPUT_SCHEMA,
            max_tokens=prompts.rules_budget(len(group) if group else
                                            len(targets(schema))),
        )]
    except ReplyError as error:
        if not error.truncated or depth >= SPLIT_DEPTH or not group or len(group) < 2:
            raise
        middle = len(group) // 2
        return (_ask(client, schema, group[:middle], depth=depth + 1)
                + _ask(client, schema, group[middle:], depth=depth + 1))


def propose(schema: FormSchema, *, client: Client | None = None,
            settings: Settings | None = None,
            sample: int = CHECK_SAMPLE, seed: int = CHECK_SEED,
            max_spend: float | None = None,
            size: int = BATCH_TARGETS) -> Proposals:
    """Ask for this form's rules, then disbelieve the answer until it checks out.

    A form with more fields than one answer can hold is asked about in
    groups. Every call sees the whole form - a rule's worth is that it links
    two fields, and a model shown half a form would invent links inside that
    half - and only the fields it is asked to decide change between them.
    """
    settings = settings or Settings.resolve("rules")
    groups = batches(schema, size)
    est = estimate(schema, settings, size)
    cost.enforce(est, max_spend)

    client = client or Client(settings)
    replies: list[Reply] = []
    for group in groups:
        replies.extend(_ask(client, schema, group if len(groups) > 1 else None))

    proposals: list[Proposal] = []
    unreadable: list[str] = []
    for reply in replies:
        got, bad = parse(reply.data)
        proposals.extend(got)
        unreadable.extend(bad)

    verdicts = check(schema, proposals, sample=sample, seed=seed)

    for message in unreadable:
        verdicts.append(Verdict(
            Proposal(field="(unreadable)", sources=(), table={}, otherwise=(),
                     confidence=0.0, why=""),
            problem=message,
        ))

    usage = {
        "input_tokens": sum(r.input_tokens for r in replies),
        "output_tokens": sum(r.output_tokens for r in replies),
        "calls": len(replies),
    }
    return Proposals(
        schema_name=schema.name,
        verdicts=verdicts,
        model=replies[0].model or settings.model,
        usage=usage,
        estimate=est,
    )


# --------------------------------------------------------------------------
# Applying, as a separate step on purpose
# --------------------------------------------------------------------------


def apply(spec: dict[str, Any], proposals: list[Proposal]) -> tuple[dict[str, Any], list[str]]:
    """Merge kept rules into a field spec document.

    Returns a new document and a list of what could not be merged. Never
    overwrites a rule the spec already declares: a rule somebody wrote by hand
    outranks one a model proposed, always.
    """
    if "fields" not in spec:
        raise ValueError("that file is not a field spec: it has no 'fields' list")

    merged = {k: (list(v) if isinstance(v, list) else v) for k, v in spec.items()}
    merged["fields"] = [dict(entry) for entry in spec["fields"]]
    by_name = {entry.get("name"): entry for entry in merged["fields"]}

    skipped: list[str] = []
    for proposal in proposals:
        entry = by_name.get(proposal.field)
        if entry is None:
            skipped.append(f"{proposal.field}: not in this spec")
            continue
        if entry.get("follows") or entry.get("derived"):
            skipped.append(f"{proposal.field}: the spec already declares a rule")
            continue
        entry.update(proposal.spec_entry())
    return merged, skipped
