// Northwind Mutual's page. The FillerAI parts are BotChat and ChatWidget; the
// rest is the application's own: its record, its forms, its submit paths.
import { BotChat, ChatWidget } from "/fillerai.js";

const $ = (id) => document.getElementById(id);
let me = null;

// ------------------------------------------------------------ the portal

async function load() {
  me = await (await fetch("/api/me")).json();
  const c = me.customer;
  $("who").textContent = `${c.full_name} · ${c.customer_id}`;
  const shown = [
    ["Name", c.full_name], ["Email", c.email], ["Policy", c.policy_number],
    ["Address", [c.street_address, c.unit].filter(Boolean).join(", ")],
    ["", [c.city, c.state, c.postal_code].filter(Boolean).join(" ")], ["", c.country],
  ];
  $("onfile").innerHTML = shown.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd>`).join("");
  const rows = [...me.requests].reverse().slice(0, 6);
  $("requests").innerHTML = rows.length
    ? rows.map((r) => `<tr><td><code>${esc(r.reference)}</code></td><td>${esc(r.kind)}`
        + `<div class="dim">${esc(r.detail)}</div></td><td class="dim">${esc(when(r.at))}</td></tr>`).join("")
    : `<tr><td colspan="3" class="dim">Nothing yet. Try the chat: `
      + `"please update my city from SFO to Irvine and house no 1429 Silverstein".</td></tr>`;
}

function formFor(template) {
  return document.querySelector(`.form-card[data-template="${template}"] form`);
}

// A form starts from what is on file, as an edit form would.
function resetForm(form) {
  for (const input of form.elements) {
    if (!input.name) continue;
    input.value = me.customer[input.name] ?? "";
    input.classList.remove("from-chat");
    const was = input.parentElement.querySelector(".was");
    if (was) was.remove();
  }
  banner(form, "");
}

function banner(form, text, tone = "") {
  const node = form.parentElement.querySelector(".banner");
  node.hidden = !text;
  node.textContent = text;
  node.className = `banner ${tone}`;
}

function fill(form, values, changes) {
  resetForm(form);
  for (const [name, value] of Object.entries(values)) {
    const input = form.elements[name];
    if (!input) continue;
    if (input.tagName === "SELECT" && ![...input.options].some((o) => o.value === value)) {
      input.append(new Option(value, value));
    }
    input.value = value;
    if (changes[name]) {
      input.classList.add("from-chat");
      const was = document.createElement("span");
      was.className = "was";
      was.textContent = changes[name].before ? `was ${changes[name].before}` : "new";
      input.after(was);
    }
  }
}

// The application's own submit path. The chat and the Save buttons both end
// up here, so there is one set of rules for what may be saved.
async function submit(path, values) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `failed (${response.status})`);
  await load();
  return body.reference;
}

for (const form of document.querySelectorAll(".form-card form")) {
  form.addEventListener("reset", (event) => {
    event.preventDefault();
    resetForm(form);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    try {
      const reference = await submit(form.dataset.path, values);
      resetForm(form);
      banner(form, `Saved. Your reference is ${reference}.`, "good");
      // If the chat handed this form over, close that conversation too.
      const template = form.closest(".form-card").dataset.template;
      const last = chat.last;
      if (last && last.conversation.status === "handed_off" && last.intent.template === template) {
        chat.report("submitted", { reference }).catch(() => {});
      }
    } catch (error) {
      banner(form, error.message, "bad");
    }
  });
}

// -------------------------------------------------------------- the chat

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
  onEffect: async (effect, chat) => {
    const form = formFor(effect.template);
    if (effect.type === "fill_form") {
      if (!form) return;
      fill(form, effect.values, effect.changes || {});
      banner(form, "Filled in from the chat. Check it, then save it.", "info");
      form.closest(".form-card").scrollIntoView({ behavior: "smooth", block: "center" });
      await chat.report("filled");
      return;
    }
    // submit: through the same path the Save button uses.
    try {
      if (!form) throw new Error("this application has no form for that yet");
      const reference = await submit(form.dataset.path, effect.values);
      resetForm(form);
      banner(form, `Saved from the chat. Your reference is ${reference}.`, "good");
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
      greeting: `Hi ${me.customer.full_name.split(" ")[0]}! I can change your address `
        + "or send you a policy document. Type, or press the microphone.",
      // A new chat also puts the forms back to what is on file.
      onReset: () => document.querySelectorAll(".form-card form").forEach(resetForm),
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

await load();
document.querySelectorAll(".form-card form").forEach(resetForm);
openChat(true);
