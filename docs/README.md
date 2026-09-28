# AIrForms documentation

The [top-level README](../README.md) is the tour: what this is, and what each
stage looks like when you run it. These are the documents you want open when
you are changing something, deciding something, or trying to find out what is
already known.

## Reading paths

| You are… | Read, in order |
|---|---|
| new to AIrForms | [overview.md](overview.md), then the [README](../README.md) tour, then [process.md](process.md) |
| explaining it to stakeholders | [algorithms.md](algorithms.md) Part 1, [nlp-and-chatbot.md](nlp-and-chatbot.md), [overview.md](overview.md) |
| about to change the code | [overview.md](overview.md), [architecture.md](architecture.md), [reference/modules.md](reference/modules.md), [pending.md](pending.md) |
| connecting another application | [integration.md](integration.md), [bot-builder.md](bot-builder.md), [reference/http-api.md](reference/http-api.md) |
| running it for other people | [operations.md](operations.md), [security.md](security.md), [reference/cli.md](reference/cli.md) |
| deciding what to build next | [pending.md](pending.md), [assumptions.md](assumptions.md), the analyses below |

## Start here

**[overview.md](overview.md)** — the whole system on a few pages: what it is
for, the parts and how a request travels through them (with diagrams), who
can do what, the design rules that explain most decisions, and where to read
next.

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

## How it thinks

**[algorithms.md](algorithms.md)** — how AIrForms learns to fill a form, in
two parts. For everyone: what kind of problem autofill is, why clustering such
as K-means is not the tool, the four layers of a prediction, the six engines
in plain words, how the numbers are checked, and the questions stakeholders
ask. For engineers: every technique precisely, with its parameters, and the
alternatives worth considering with a recommendation.

**[nlp-and-chatbot.md](nlp-and-chatbot.md)** — how the chat understands what
a person types or says: the flow from keyboard or microphone to a filled
form, the local reader's language pipeline step by step with real examples,
the optional language-model reader and speech transcription, what leaves the
machine with each, and the limits.

## Operating it

**[operations.md](operations.md)** — requirements, installing, starting the
server and what it prints, every environment variable, where the data is and
how to back it up, upgrading, the sample application, and a troubleshooting
table of the problems actually met so far.

**[security.md](security.md)** — what is protected and how, exactly what can
leave the machine and under which switch, passwords, sessions, tokens and
keys, the two HTTP surfaces, data at rest, and a checklist for exposing it
beyond localhost.

## Reference

Looked up rather than read through. Each is checked against the code at
0.15.0.

**[reference/cli.md](reference/cli.md)** — every command and subcommand, every
option with its default, and every environment variable.

**[reference/http-api.md](reference/http-api.md)** — every route on `/api`,
`/v1` and the sample application: who may call it, what it takes, what it
answers and how it refuses.

**[reference/data-formats.md](reference/data-formats.md)** — the field schema
and field spec, datasets, model files, library entries and lineage, the
database tables and migrations, API tokens, and bot templates.

**[reference/modules.md](reference/modules.md)** — the package module by
module: what each is responsible for, its main functions, what it imports,
the Python library API, and which tests cover what.

**[glossary.md](glossary.md)** — the words these documents use with a specific
meaning.

**[integration.md](integration.md)** — calling AIrForms from another
application: every `/v1` endpoint and what it answers, how an API token works
and why it is not the UI's cookie, when a browser on another origin is let in,
and how to bind a model to a form in plain JavaScript or in React.

**[bot-builder.md](bot-builder.md)** — the contract between a chat window in
another application and the bot service: templates, the one `input` that
typing, speaking and clicking all arrive as, the before/after and suggested
actions in a reply, the `effect` the application acts on, and conversations
that carry their own state.

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
