# User guide

AIrForms turns a form your agents fill by hand into one that mostly fills
itself. You give it the form, it works out what each field means, invents
realistic records for it (or cleans real ones you upload), learns from those
records, and then completes the rest of the form from the first few answers
an agent types.

The app walks through that in five stages, plus a library of everything you
have made and a Bot Builder for chat. Each stage ends with a link to the next,
so you can go from a form to a measured saving without using the menu. This
guide has one section per screen; the same sections open in the app's help
panel, from the **Help** tab on the right edge of every screen.

<!-- panel: source -->
## Source: bring in the form

**What this screen is for.** Everything starts from a form. AIrForms reads
its markup, or a field list if the markup cannot be shared, and works out
what each field is.

### How to

1. Paste the form's HTML into the box, drop an `.html` file onto it, or use
   **Choose file**.
2. If you only have a list of fields, switch to **Field spec (JSON)** first.
   A schema you downloaded earlier works here too.
3. No form to hand? Pick one of the **Examples** on the right, or switch to
   **Records (CSV)** and drop a file of past submissions: each column becomes
   a field, and the records wait on the Data step to be cleaned.
4. Press **Read the form**. You land on the Schema screen with every field
   it found.

### Tips

- The form never leaves this machine. Only the server you are signed in to
  reads it.
- Multi-screen forms are fine: fieldsets and sections become groups.
- Anything you read is kept in the **Library**, so you can come back to it.

**Next:** Schema, to check what each field was taken to mean.

<!-- panel: schema -->
## Schema: check what each field means

**What this screen is for.** Each field's *meaning* (a postal code, a date of
birth, a policy number) decides what gets generated for it and what the model
can learn. This is where you confirm it.

### How to

1. Read the summary tiles: how many fields, how many groups, how many need a
   look.
2. Tick **Only ones to review** to see just the fields the reader was unsure
   about.
3. Change a field's **Means** where it is wrong. Your choice is final and
   will not be overruled later.
4. Optionally press **Propose rules** to have a language model suggest how
   fields follow from each other (a city from a postal code, a plan from a
   tier). Tick the ones that are true of your form and apply them. This needs
   a key in Settings.
5. Press **Generate data** to move on.

### Tips

- Rules are what make generated records hang together, and hanging together
  is what the model learns from. A form with good rules autofills far more.
- **Download schema** saves the JSON contract every later stage uses.

**Next:** Data, to generate sample records from this schema or upload real
ones.

<!-- panel: generate -->
## Data: generate sample records, or upload real ones

**What this screen is for.** The records a model learns from. Companies
rarely share real form data, so AIrForms can invent it: every generated
record is one coherent imaginary person whose values fit the form's
constraints and its rules. When real past submissions do arrive, upload them
here instead, to train on them or to test a model with them.

### How to: generate

1. Choose how many **Records** you want. A few hundred is a good start for
   training; twenty is enough to look at.
2. Leave **Seed** as it is to get the same records again, or clear it for
   new ones each time.
3. **Blank rate** is how often an optional field is left empty, as it would
   be in real life.
4. Keep **Non-issuable identifiers** ticked so that ID numbers fall in ranges
   that cannot belong to a real person.
5. Press **Generate**. Every record is checked before you see it, and any
   problem is listed above the table.

### How to: upload real records

1. Switch to **Upload real records** and choose or drop a CSV, JSON or
   NDJSON file.
2. Check **Which field is each column?** Columns are matched by name and
   label; change any guess, or pick *leave out*.
3. Read **Cleaning**. Each fix says how many cells or rows it changed, with an
   example. Untick any you do not want. *Empty values the form would still
   reject* and *Drop rows missing a required field* throw information away,
   so they are off until you tick them.
4. Anything still wrong is listed under the fixes, usually a column mapped to
   the wrong field.
5. Press **Save cleaned records**. They go into the Library as real records
   under this form.

### When it is done

A **Next step** bar appears: **Train now** trains on these records straight
away, and **Go to Train** takes them there to choose how first. With real
records and a model already loaded, it also offers **Test the current
model**, which scores that model on them on the Simulate step. You can export
generated records as CSV, JSON or NDJSON.

**Next:** Train, to learn the form from these records.

<!-- panel: train -->
## Train: teach a model the form

**What this screen is for.** The model learns which fields can be predicted
from which, so that after an agent types the first few, the rest can be
filled in. It also learns which fields it should *not* guess.

### How to

1. Pick **How it learns**. Each engine shows what it will do on the right;
   the default is a good place to start.
2. Keep **Use verified rules as well** ticked. Rules are arithmetic, and they
   apply whichever engine you choose.
3. **Fields to ask for** is how many fields the agent types before the model
   fills the rest. **Confidence to fill** is how sure it must be to fill a
   field rather than leave it.
4. Press **Train**. The log shows what it is doing as it does it, and ends
   with the script that repeats the run.
5. Try it in **Type the first few**: fill the suggested fields and watch the
   rest of the form complete underneath.

### Reading the result

- **The model can fill / yours to type** splits the form in two.
- **Right when it fills** is measured on records the model never saw.
- **What the model learned** lists every field and what answers it: a rule,
  other fields, its usual value, or nothing.

**Next:** Simulate, to play the model against a form it has never seen and
see what it saves.

<!-- panel: simulate -->
## Simulate: measure the saving

**What this screen is for.** A form the model has never seen, drawn from its
own schema, filled the way an agent would fill it. The panel on the right
counts what the model saved.

### How to

1. Type into the highlighted boxes, or press **Type the first few for me**.
2. Watch the rest of the form fill. Colours show what you typed, what the
   model filled, what it only suggested, and what is still yours.
3. Keep **Mark against the real form** ticked to see which filled values were
   right.
4. Press **New form** for another case, or run **Over many forms** to get a
   number you can quote.

### Tips

- **Filled in by** names the model and the source, schema and records it was
  made from. Each name opens that entry in the Library.
- The seconds are modelled from keystrokes and fixed assumptions, listed at
  the bottom. They are an estimate, not a stopwatch.
- **Test on real records** scores the model on records that were really
  submitted: pick a dataset under **Records**, press **Test**. No real
  records yet? **Upload some** takes you to the Data step. It warns you if
  the model learned from those same records.

**Next:** back to Train to try another engine, or Bots to put the model
behind a chat.

<!-- panel: library -->
## Library: everything you have made

**What this screen is for.** Every source, schema, dataset, model, training
script and bot template is kept, with what it was made from.

### How to

1. Filter by kind with the buttons along the top.
2. **Open** an entry to pick up where you left off. A dataset opens on
   Data, a model on Train, a template on Bots.
3. **Download** an entry to keep a copy, or **Delete** one you no longer
   need. Deleting a model takes its training script with it.

### Tips

- With accounts on, your library is yours alone. Other people cannot see it.
- Deleting something that other entries were made from asks first.
- The lineage under each entry is the chain it was made from, so you can
  always tell which form a model belongs to.

<!-- panel: bots -->
## Bots: chat that fills forms

**What this screen is for.** A template is one kind of request, such as an
address change or a document request, and the words people use for it.
Another application's chat window sends what its users type, say or click,
and gets back the template, the values and what to offer next.

### How to

1. Pick a template, or add one: **New**, a starter, or **From a schema** to
   build one from a form you have read.
2. Fill in the phrases people use for it and the fields it collects. Choose
   the finishing actions: fill the form for a person to check, submit, or
   both.
3. Optionally attach an autofill model, so the related fields are filled too.
4. Try it in the chat on the right. The sample form follows the
   conversation as you talk.
5. Press **Save**. Applications reach it by its key over `/v1/bot/turn`.

### Tips

- **Sample app** in the menu opens a separate demo application whose forms
  are your templates.
- A field that *follows* others is asked again when one of them changes: a
  new city makes the ZIP code on file wrong.

<!-- panel: account -->
## Settings

**What this screen is for.** The optional language-model key, API tokens for
other applications, your password, the documentation access code, and, for
administrators, everybody's accounts.

### How to

1. **Language model**: paste an Anthropic or OpenAI key to turn on
   **Propose rules**. A key typed here is kept in memory only.
2. **API tokens**: issue one per application that will call `/v1`. The
   secret is shown once.
3. **Documentation access**: an administrator sets the code people need to
   open the documentation at `/docs`, by typing one and pressing **Set code**
   or with **Make one up** (shown once). Until one is set, the docs are
   closed; **Close the docs** closes them again. Changing the code signs
   every browser out of the docs.
4. **Change your password** signs out your other browsers.

### Tips

- Share the documentation code the way you would share a meeting link: it
  opens the docs, not the app.
- Revoking an API token changes nothing about how you sign in.
