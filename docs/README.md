# FillerAI documentation

The [top-level README](../README.md) is the tour: what this is, and what each
stage looks like when you run it. These are the documents you want open when
you are changing something, deciding something, or trying to find out what is
already known.

## Start here

**[architecture.md](architecture.md)** — what the pieces are, which way they
point, and the boundaries that are not allowed to move. The shared schema
contract, the four stages, the library and its two implementations, the
storage interface, the two HTTP surfaces, the LLM import fence, and a list of
the invariants a change should not quietly break.

**[process.md](process.md)** — the same system as a sequence of things you do,
from a form nobody has seen to a number saying what the model saved. Every
command, what each option is for, how to choose an engine, what the reports
mean, and the development process around it (tests, branches, the two rules
that outrank convenience).

**[assumptions.md](assumptions.md)** — everything the system takes as given,
grouped by what it is about: the environment, forms, generated data, the
saving, the model, the LLM features, operations, the test suite. Each entry
says what is assumed, where it lives, and what breaks if it is false. The five
effort constants behind every "X% less work" are §4.

**[pending.md](pending.md)** — what is missing, what is known to be broken, and
what was deliberately not built, each with its evidence and what it would take
to resolve. Read this before picking up work.

## Reference

**[integration.md](integration.md)** — calling FillerAI from another
application: every `/v1` endpoint and what it answers, how an API token works
and why it is not the UI's cookie, when a browser on another origin is let in,
and how to bind a model to a form in plain JavaScript or in React.

**[training-and-scale.md](training-and-scale.md)** — what a training run does
stage by stage and what each stage costs, what it would take to serve a model
against a real production form, and measured behaviour at 20,000 records
including the classification limit that silently drops high-cardinality
fields.

## Analyses

These record a decision and the measurements behind it. They are history as
much as documentation — `weight-based-training.md` opens with a section saying
where it turned out to be wrong once the thing was built.

**[weight-based-training.md](weight-based-training.md)** — what an engine that
learns weights rather than counts would cost under the no-dependency rule,
measured against the ones that existed, and which candidate was worth
building. The `linear` engine and the learned vote weights both came out of
this.

**[llm-modelling.md](llm-modelling.md)** — where a language model would earn
its place in the pipeline, what it would cost per form and per year, and why
the ceiling on autofill turns out to be information rather than model quality.
Verdict: yes at build time, no at fill time.

**[llm-implementation-plan.md](llm-implementation-plan.md)** — how the two
worthwhile pieces would be built: the module layout that keeps the core free
of any network call, the validation gate that treats a proposal as a proposal,
how a non-deterministic component is tested inside a deterministic suite, and
the measured gate each phase must clear before the next starts. Phases 0 and 1
shipped; 2, 3 and 4 have not.
