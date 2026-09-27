// Northwind Mutual's page. The FillerAI parts are BotChat and ChatWidget; the
// rest is the application's own: its record, its forms, its submit path.
//
// The forms are not written into the page: there is one for each template
// the FillerAI bot service has, drawn from /api/templates when the page
// loads. Once drawn they are ordinary forms, filled by hand or by the chat.
import { BotChat, ChatWidget } from "/fillerai.js";

const $ = (id) => document.getElementById(id);
let me = null;
let templates = [];

// ------------------------------------------------------------ the portal

async function load() {
  me = await (await fetch("/api/me")).json();
  const c = me.customer;
  $("who").textContent = `${c.full_name} · ${c.customer_id}`;
  const shown = [
    ["Name", c.full_name], ["Email", c.email], ["Phone", c.phone], ["Policy", c.policy_number],
    ["Address", [c.street_address, c.unit].filter(Boolean).join(", ")],
    ["", [c.city, c.state, c.postal_code].filter(Boolean).join(" ")], ["", c.country],
  ];
  $("onfile").innerHTML = shown.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd>`).join("");
  const rows = [...me.requests].reverse().slice(0, 6);
  const example = templates[0] ? (templates[0].examples || [])[0] : "";
  $("requests").innerHTML = rows.length
    ? rows.map((r) => `<tr><td><code>${esc(r.reference)}</code></td><td>${esc(r.kind)}`
        + `<div class="dim">${esc(r.detail)}</div></td><td class="dim">${esc(when(r.at))}</td></tr>`).join("")
    : `<tr><td colspan="3" class="dim">Nothing yet.${example
        ? ` Try the chat: "${esc(example)}".` : ""}</td></tr>`;
}

// One card per template, after the two the page always has.
async function drawForms() {
  const response = await fetch("/api/templates");
  const body = await response.json();
  if (!response.ok) {
    showNoForms(`FillerAI didn't give this application its templates: ${body.error || response.status}.`);
    return;
  }
  templates = body.templates;
  if (!templates.length) {
    showNoForms("FillerAI's bot service has no templates yet. Add some on its Bots tab"
      + " (the starters are a quick way in), then reload this page.");
    return;
  }
  for (const template of templates) {
    $("layout").append(formCard(template));
    const link = document.createElement("a");
    link.href = `#form-${template.key}`;
    link.textContent = template.name;
    $("nav").insertBefore(link, $("nav").lastElementChild);
  }
}

function showNoForms(text) {
  $("noForms").hidden = false;
  $("noFormsText").textContent = text;
  if (me && me.fillerai_page) {
    const link = document.createElement("a");
    link.href = me.fillerai_page;
    link.target = "_blank";
    link.textContent = " Open FillerAI.";
    $("noFormsText").append(link);
  }
}

function formCard(template) {
  const card = document.createElement("section");
  card.className = "card form-card";
  card.id = `form-${template.key}`;
  card.dataset.template = template.key;
  card.innerHTML = `<h2>${esc(template.name)}<small>${esc(template.description || "")}</small></h2>
    <div class="banner" hidden></div>
    <form autocomplete="off"><div class="grid"></div>
      <div class="form-foot">
        <button type="reset" class="quiet">Put back what is on file</button>
        <button type="submit">Submit</button>
      </div></form>`;
  const grid = card.querySelector(".grid");
  for (const field of template.fields) {
    const label = document.createElement("label");
    if (field.semantic_type === "street_address" || field.semantic_type === "full_name") {
      label.className = "wide";
    }
    label.append(field.label || field.name);
    if (field.required) {
      const star = document.createElement("span");
      star.className = "req";
      star.textContent = " *";
      label.append(star);
    }
    let input;
    if (field.options && field.options.length) {
      input = document.createElement("select");
      input.append(new Option("", ""));
      for (const option of field.options) input.append(new Option(option, option));
    } else {
      input = document.createElement("input");
      if (field.example) input.placeholder = `e.g. ${field.example}`;
    }
    input.name = field.name;
    input.required = Boolean(field.required);
    label.append(input);
    grid.append(label);
  }
  const form = card.querySelector("form");
  form.addEventListener("reset", (event) => {
    event.preventDefault();
    resetForm(form);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    try {
      const reference = await submit(template.key, values);
      resetForm(form);
      banner(form, `Saved. Your reference is ${reference}.`, "good");
      // If the chat handed this form over, close that conversation too.
      const last = chat.last;
      if (last && last.conversation.status === "handed_off" && last.intent.template === template.key) {
        chat.report("submitted", { reference }).catch(() => {});
      }
    } catch (error) {
      banner(form, error.message, "bad");
    }
  });
  return card;
}

function formFor(template) {
  return document.querySelector(`.form-card[data-template="${CSS.escape(template)}"] form`);
}

// A form starts from what is on file, as an edit form would.
function resetForm(form) {
  for (const input of form.elements) {
    if (!input.name) continue;
    input.value = me.customer[input.name] ?? "";
    input.classList.remove("from-chat", "needed");
    const was = input.parentElement.querySelector(".was");
    if (was) was.remove();
  }
  form.closest(".form-card").classList.remove("chatting");
  banner(form, "");
}

function banner(form, text, tone = "") {
  const node = form.parentElement.querySelector(".banner");
  node.hidden = !text;
  node.textContent = text;
  node.className = `banner ${tone}`;
}

function setValue(input, value) {
  if (input.tagName === "SELECT" && value && ![...input.options].some((o) => o.value === value)) {
    input.append(new Option(value, value));
  }
  input.value = value ?? "";
}

// The chat's picture of the form, put into the real one: every value it
// has, what each changed from, and the ones it still needs.
function showChatForm(form, reply) {
  for (const row of reply.form.fields) {
    const input = form.elements[row.name];
    if (!input) continue;
    setValue(input, row.after ?? (row.status === "outdated" ? "" : input.value));
    input.classList.toggle("from-chat", row.status === "changed");
    input.classList.toggle("needed", row.status === "missing" || row.status === "outdated");
    const old = input.parentElement.querySelector(".was");
    if (old) old.remove();
    const change = reply.form.changes[row.name];
    if (change) {
      const was = document.createElement("span");
      was.className = "was";
      was.textContent = change.before ? `was ${change.before}` : "new";
      input.after(was);
    }
  }
}

// The application's own submit path. The chat and the Submit buttons both
// end up here, so there is one set of rules for what may be saved.
async function submit(template, values) {
  const response = await fetch(`/api/submit/${encodeURIComponent(template)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `failed (${response.status})`);
  await load();
  return body.reference;
}

// -------------------------------------------------------------- the chat

let chatOn = null;

// Every turn goes to this application's server, which adds the customer's
// record and the API token before passing it to FillerAI.
const chat = new BotChat(null, {
  send: async (body) => {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const reply = await response.json();
    if (!response.ok) throw new Error(reply.error || `the assistant is unavailable (${response.status})`);
    return reply;
  },
  // As the conversation goes, the form it is about fills in on the page.
  // Moving to another template (someone asks for a document half way
  // through an address change) moves to that form.
  onReply: (reply) => {
    if (reply.stale || !reply.form) return;
    const form = formFor(reply.form.template);
    const status = reply.conversation.status;
    if (!form || status === "done" || status === "cancelled") return;
    if (chatOn !== reply.form.template) {
      const before = chatOn && formFor(chatOn);
      if (before) resetForm(before);
      chatOn = reply.form.template;
      form.closest(".form-card").classList.add("chatting");
      form.closest(".form-card").scrollIntoView({ behavior: "smooth", block: "center" });
    }
    showChatForm(form, reply);
    if (status === "collecting" || status === "ready") {
      banner(form, "The chat is filling this in as you talk.", "info");
    }
  },
  onEffect: async (effect, chat) => {
    const form = formFor(effect.template);
    if (effect.type === "fill_form") {
      if (!form) return;
      banner(form, "Filled in from the chat. Check it, then submit it.", "info");
      form.closest(".form-card").scrollIntoView({ behavior: "smooth", block: "center" });
      await chat.report("filled");
      return;
    }
    // submit: through the same path the Submit button uses.
    try {
      if (!form) throw new Error("this application has no form for that yet; reload the page");
      const reference = await submit(effect.template, effect.values);
      resetForm(form);
      chatOn = null;
      banner(form, `Submitted from the chat. Your reference is ${reference}.`, "good");
      await chat.report("submitted", { reference });
    } catch (error) {
      await chat.report("submit_failed", { message: error.message });
    }
  },
});

let widget = null;

function openChat(open) {
  $("dock").hidden = !open;
  document.body.classList.toggle("chat-open", open);
  $("launcher").setAttribute("aria-expanded", String(open));
  $("launcher").textContent = open ? "Close chat" : "Chat with us";
  if (open && !widget) {
    const names = templates.map((t) => t.name.toLowerCase());
    widget = new ChatWidget($("chat"), chat, {
      title: "Northwind assistant",
      // With --server-speech the recording goes through this server to
      // FillerAI's /v1/bot/transcribe instead of the browser's recogniser.
      transcribe: me.server_speech ? async (audio) => {
        const response = await fetch("/api/transcribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(audio),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `failed (${response.status})`);
        return body;
      } : null,
      greeting: `Hi ${me.customer.full_name.split(" ")[0]}! `
        + (names.length ? `I can help with ${joinWords(names)}. ` : "")
        + "Type, or press the microphone.",
      // A new chat also puts the forms back to what is on file.
      onReset: () => {
        chatOn = null;
        document.querySelectorAll(".form-card form").forEach(resetForm);
      },
    });
  }
  if (open) widget.input.focus();
}

$("launcher").addEventListener("click", () => openChat($("dock").hidden));

// ---------------------------------------------------------------- helpers

function esc(text) {
  return String(text ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function when(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function joinWords(words) {
  return words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} or ${words.at(-1)}`;
}

me = await (await fetch("/api/me")).json();
await drawForms();
await load();
document.querySelectorAll(".form-card form").forEach(resetForm);
openChat(true);
