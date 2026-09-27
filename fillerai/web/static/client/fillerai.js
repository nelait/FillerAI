/**
 * FillerAI browser and Node client.
 *
 * One file, no dependencies, no build step. It is an ES module, so a React
 * or Vue application imports it and a bundler treats it like any other
 * source file; a plain page loads it with `<script type="module">`, and for
 * a page that also has classic scripts it puts `FillerAI` on `globalThis`
 * on the way past.
 *
 *   import { FillerAI } from "./fillerai.js";
 *
 *   const filler = new FillerAI({
 *     baseUrl: "http://localhost:8000",
 *     token: "flr_...",
 *   });
 *   const { models } = await filler.models();
 *   const answer = await filler.suggest(models[0].id, { policy_number: "A-1" });
 *   answer.values;       // what to put in the boxes
 *   answer.suggestions;  // each one with its confidence and its reason
 *
 * The chat pieces - {@link BotChat}, {@link SpeechInput} and
 * {@link ChatWidget} - talk to the bot service; see docs/bot-builder.md.
 *
 * Apart from those, the only thing here that is not a thin wrapper around
 * `fetch` is {@link FormBinder}, and that is because binding a model to a real form is
 * the part every integration would otherwise write for itself: watch the
 * fields a person types into, ask once they pause, fill what the model is
 * sure about, and never overwrite something typed by hand.
 */

/** Anything the service refused, with the reason it gave. */
export class FillerAIError extends Error {
  constructor(message, { status = 0, code = "error", body = null } = {}) {
    super(message);
    this.name = "FillerAIError";
    this.status = status;
    this.code = code;
    this.body = body;
  }

  /** True when the credential is the problem, rather than the request. */
  get isAuth() {
    return this.status === 401 || this.status === 403;
  }
}

const DEFAULT_BASE = "http://localhost:8000";

function trimSlash(url) {
  return String(url || "").replace(/\/+$/, "");
}

/**
 * A connection to one FillerAI service.
 *
 * Every method returns a promise of the decoded reply and throws
 * {@link FillerAIError} on anything else, so a caller never has to look at a
 * status code to find out whether it worked.
 */
export class FillerAI {
  /**
   * @param {object} options
   * @param {string} [options.baseUrl] Where the service is.
   * @param {string} [options.token] An API token (`flr_...`).
   * @param {number} [options.threshold] Confidence below which a suggestion
   *   is reported but not offered. Omitted means the service's own default,
   *   which is the one the model was calibrated against.
   * @param {function} [options.fetch] For tests, or for Node before 18.
   * @param {number} [options.timeout] Milliseconds before a call is aborted.
   */
  constructor({ baseUrl = DEFAULT_BASE, token = "", threshold = null,
                fetch: fetchImpl = null, timeout = 15000 } = {}) {
    this.baseUrl = trimSlash(baseUrl);
    this.token = token || "";
    this.threshold = threshold;
    this.timeout = timeout;
    this._fetch = fetchImpl || (typeof fetch === "function" ? fetch.bind(globalThis) : null);
    if (!this._fetch) {
      throw new FillerAIError("no fetch available; pass one in options.fetch");
    }
  }

  /** Swap the credential without rebuilding everything that holds this. */
  setToken(token) {
    this.token = token || "";
    return this;
  }

  // -- the calls --------------------------------------------------------

  /** Is the service there, and what version is it. Needs no token. */
  health() {
    return this._call("GET", "/v1/health");
  }

  /** Every model this token may ask about. */
  models() {
    return this._call("GET", "/v1/models");
  }

  /**
   * One model in full: its fields, what it can predict each of them from,
   * and which few to ask a person for first.
   */
  model(modelId) {
    return this._call("GET", `/v1/models/${encodeURIComponent(modelId)}`);
  }

  /**
   * What the model would put in the fields that are still empty.
   *
   * @param {string} modelId
   * @param {object} observed What is filled in already, by field name.
   * @param {object} [options]
   * @param {number} [options.threshold] Override the client's default.
   * @param {string[]} [options.fields] Only ask about these.
   */
  suggest(modelId, observed = {}, { threshold, fields } = {}) {
    return this._call("POST", `/v1/models/${encodeURIComponent(modelId)}/suggest`, {
      observed,
      ...this._threshold(threshold),
      ...(fields ? { fields } : {}),
    });
  }

  /** The record as the model would hand it back, confident answers only. */
  fill(modelId, observed = {}, { threshold } = {}) {
    return this._call("POST", `/v1/models/${encodeURIComponent(modelId)}/fill`, {
      observed,
      ...this._threshold(threshold),
    });
  }

  /** {@link fill}, over many partial records in one round trip. */
  batch(modelId, records = [], { threshold } = {}) {
    return this._call("POST", `/v1/models/${encodeURIComponent(modelId)}/batch`, {
      records,
      ...this._threshold(threshold),
    });
  }

  // -- the bot service (docs/bot-builder.md) -----------------------------

  /** Every bot template this token may use. */
  templates() {
    return this._call("GET", "/v1/templates");
  }

  /** One template in full. */
  template(key) {
    return this._call("GET", `/v1/templates/${encodeURIComponent(key)}`);
  }

  /** Create a template, or replace the one with the same key. */
  saveTemplate(template) {
    return this._call("POST", "/v1/templates", { template });
  }

  /** Remove a template by key. */
  deleteTemplate(key) {
    return this._call("POST", `/v1/templates/${encodeURIComponent(key)}/delete`, {});
  }

  /**
   * One turn of a conversation: `{input, state, context}` in, a reply out.
   * Most callers want {@link BotChat}, which keeps the state for them.
   */
  turn(body) {
    return this._call("POST", "/v1/bot/turn", body);
  }

  /**
   * Bind a model to a `<form>`: fill it as the person types, and leave
   * anything they typed themselves alone. See {@link FormBinder}.
   */
  async bind(form, modelId, options = {}) {
    const described = await this.model(modelId);
    const binder = new FormBinder(this, modelId, form, described, options);
    binder.start();
    return binder;
  }

  // -- the plumbing -----------------------------------------------------

  _threshold(given) {
    const value = given === undefined ? this.threshold : given;
    return value === null || value === undefined ? {} : { threshold: value };
  }

  async _call(method, path, body) {
    const headers = { Accept: "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller && this.timeout
      ? setTimeout(() => controller.abort(), this.timeout)
      : null;

    let response;
    try {
      response = await this._fetch(this.baseUrl + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller ? controller.signal : undefined,
      });
    } catch (error) {
      // A network failure and a CORS refusal arrive here as the same opaque
      // TypeError, so the message says both rather than guessing.
      throw new FillerAIError(
        `could not reach ${this.baseUrl}: ${error.message}. If the page is on `
        + "another origin, the server needs --cors-origin for it.",
        { code: "unreachable" },
      );
    } finally {
      if (timer) clearTimeout(timer);
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message = (payload && payload.error) || `${response.status} from ${path}`;
      throw new FillerAIError(message, {
        status: response.status,
        code: (payload && payload.code) || "error",
        body: payload,
      });
    }
    return payload;
  }
}

/**
 * A model bound to a real form.
 *
 * The rules it follows are the ones an agent-facing form needs and every
 * integration would otherwise reinvent:
 *
 * - **What the person typed wins.** A field they have touched is never
 *   overwritten, and is sent to the model as something it now knows.
 * - **A suggestion is marked as one.** Filled fields get a CSS class and a
 *   `data-fillerai` attribute, so the page can show which values came from
 *   the model, and a person editing one takes it back.
 * - **It asks after a pause, not per keystroke**, and only one call is ever
 *   in flight: a reply that arrives after a newer one is dropped rather than
 *   racing it into the form.
 */
export class FormBinder {
  constructor(client, modelId, form, described, {
    delay = 250,
    threshold,
    fillClass = "fillerai-filled",
    onSuggest = null,
    onError = null,
  } = {}) {
    this.client = client;
    this.modelId = modelId;
    this.form = form;
    this.described = described;
    this.delay = delay;
    this.threshold = threshold;
    this.fillClass = fillClass;
    this.onSuggest = onSuggest;
    this.onError = onError;

    /** Field names the model knows, so nothing else in the form is sent. */
    this.known = new Set((described.fields || []).map((f) => f.name));
    /** Fields a person has typed into. Never overwritten, always sent. */
    this.typed = new Set();
    /** Fields this binder wrote. Overwritten freely; cleared when wrong. */
    this.suggested = new Set();

    this._timer = null;
    this._generation = 0;
    this._onInput = this._onInput.bind(this);
  }

  start() {
    this.form.addEventListener("input", this._onInput);
    this.form.addEventListener("change", this._onInput);
    return this;
  }

  stop() {
    this.form.removeEventListener("input", this._onInput);
    this.form.removeEventListener("change", this._onInput);
    if (this._timer) clearTimeout(this._timer);
    return this;
  }

  /** What the person has actually filled in, as the model wants to hear it. */
  observed() {
    const out = {};
    for (const name of this.typed) {
      const element = this.form.elements[name];
      if (!element) continue;
      const value = readValue(element);
      if (value !== "") out[name] = value;
    }
    return out;
  }

  /** Ask now, without waiting for the pause. */
  async suggest() {
    const generation = ++this._generation;
    let answer;
    try {
      answer = await this.client.suggest(this.modelId, this.observed(), {
        threshold: this.threshold,
      });
    } catch (error) {
      if (this.onError) this.onError(error);
      else throw error;
      return null;
    }
    // A reply for an older keystroke would put back values the person has
    // already moved past.
    if (generation !== this._generation) return null;
    this.apply(answer);
    if (this.onSuggest) this.onSuggest(answer, this);
    return answer;
  }

  /** Put a reply's confident values into the form. */
  apply(answer) {
    for (const [name, value] of Object.entries(answer.values || {})) {
      if (this.typed.has(name)) continue;
      const element = this.form.elements[name];
      if (!element) continue;
      writeValue(element, value);
      mark(element, this.fillClass, true);
      this.suggested.add(name);
    }
    // A field the model no longer stands behind should not keep an answer it
    // gave two keystrokes ago.
    const offered = new Set(Object.keys(answer.values || {}));
    for (const name of [...this.suggested]) {
      if (offered.has(name) || this.typed.has(name)) continue;
      const element = this.form.elements[name];
      if (element) {
        writeValue(element, "");
        mark(element, this.fillClass, false);
      }
      this.suggested.delete(name);
    }
  }

  /** Forget everything typed and everything suggested. */
  reset() {
    // The pending ask goes too. Bumping the generation only stops a reply
    // that is already on its way; a debounce still waiting to fire would
    // start a fresh one and put the model's unconditional guesses straight
    // back into a form somebody just asked to be empty.
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    for (const name of [...this.suggested]) {
      const element = this.form.elements[name];
      if (element) {
        writeValue(element, "");
        mark(element, this.fillClass, false);
      }
    }
    this.typed.clear();
    this.suggested.clear();
    this._generation++;
  }

  _onInput(event) {
    const element = event.target;
    const name = element && element.name;
    if (!name || !this.known.has(name)) return;

    // Editing a suggestion is how a person takes it back: from here on it is
    // theirs, it is not overwritten, and the model is told about it.
    this.typed.add(name);
    this.suggested.delete(name);
    mark(element, this.fillClass, false);

    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this.suggest(), this.delay);
  }
}

/**
 * One conversation with the bot service.
 *
 * Typing, speaking and clicking a suggested action all go through
 * {@link BotChat#send}, as one `input`, so there is one path for all three.
 * Inputs run one at a time in the order they were given - unlike
 * {@link FormBinder}, a chat must never drop a message because a newer one
 * arrived.
 *
 * `send` can be given in the options to go through the host's own backend
 * instead of calling `/v1` with a token from the browser: it receives the
 * request body and returns the reply.
 */
export class BotChat {
  /**
   * @param {FillerAI|null} client
   * @param {object} [options]
   * @param {object|function} [options.current] What the host already holds,
   *   as `{field: value}`, or a function returning it (read every turn).
   * @param {string[]} [options.templates] Limit the chat to these templates.
   * @param {string} [options.template] Start on this template.
   * @param {function} [options.send] `(body) => Promise<reply>`, to proxy.
   * @param {function} [options.onReply] Called with every reply.
   * @param {function} [options.onEffect] Called when a reply carries an effect.
   * @param {function} [options.onError] Called when a turn is refused.
   */
  constructor(client, { current = null, templates = null, template = null,
                        send = null, onReply = null, onEffect = null,
                        onError = null } = {}) {
    this.client = client;
    this.current = current;
    this.templates = templates;
    this.startOn = template;
    this._send = send || ((body) => this.client.turn(body));
    this.onReply = onReply;
    this.onEffect = onEffect;
    this.onError = onError;
    /** The state the service handed back last; sent with the next turn. */
    this.state = null;
    /** The last reply, for anything that wants to redraw from it. */
    this.last = null;
    this._queue = Promise.resolve();
    this._epoch = 0;
  }

  /** Something the person typed. */
  say(text, { via = "typed", alternatives = [] } = {}) {
    return this.send({ type: "text", text, via, alternatives });
  }

  /** Something the person said, once speech recognition has a final phrase. */
  speak(text, alternatives = []) {
    return this.say(text, { via: "speech", alternatives });
  }

  /** A suggested action, by the object from `reply.actions` or its id. */
  click(action) {
    return this.send({ type: "action", action: typeof action === "string" ? action : action.id });
  }

  /** Tell the service how an effect went: `submitted`, `submit_failed`, `filled`. */
  report(event, detail = {}) {
    return this.send({ type: "event", event, detail });
  }

  /**
   * Forget the conversation; the next input starts a new one. A turn already
   * on its way when this is called is let finish but its reply is dropped,
   * so it cannot bring the old conversation back.
   */
  reset() {
    this._epoch += 1;
    this.state = null;
    this.last = null;
  }

  /** Any of the three inputs. Queued behind the one before it. */
  send(input) {
    const run = this._queue.then(() => this._turn(input));
    // A failed turn must not wedge every turn after it.
    this._queue = run.catch(() => {});
    // The effect runs once this turn is out of the queue, so a handler that
    // reports back with `report()` queues behind it instead of waiting on
    // itself.
    return run.then(async (reply) => {
      if (reply.effect && this.onEffect) await this.onEffect(reply.effect, this, reply);
      return reply;
    });
  }

  async _turn(input) {
    const current = typeof this.current === "function" ? this.current() : this.current;
    const context = {};
    if (current) context.current = current;
    if (this.templates) context.templates = this.templates;
    if (this.startOn && !this.state) context.template = this.startOn;
    const epoch = this._epoch;
    let reply;
    try {
      reply = await this._send({ input, state: this.state, context });
    } catch (error) {
      if (epoch === this._epoch && this.onError) this.onError(error, input);
      throw error;
    }
    // Reset while this turn was out: the reply belongs to a conversation
    // that no longer exists. Its effect is dropped with it.
    if (epoch !== this._epoch) return { ...reply, effect: null, stale: true };
    this.state = reply.state;
    this.last = reply;
    if (this.onReply) this.onReply(reply, input);
    return reply;
  }
}

/**
 * The browser's speech recognition, reduced to what a chat box needs.
 *
 * FillerAI never receives audio: the phrase is recognised by whatever engine
 * the browser uses, and arrives at the bot service as text with
 * `via: "speech"`. Where the audio goes is the browser's business - Chrome,
 * for one, sends it to Google's recogniser - which a host that cares should
 * know before it turns the microphone on. Where the browser has no recogniser,
 * `SpeechInput.supported` is false and {@link ChatWidget} hides the
 * microphone rather than offering one that does nothing.
 */
export class SpeechInput {
  static get supported() {
    return typeof globalThis !== "undefined"
      && Boolean(globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition);
  }

  /**
   * @param {object} options
   * @param {function} options.onFinal `(text, alternatives)` when a phrase ends.
   * @param {function} [options.onInterim] `(text)` while the person speaks.
   * @param {function} [options.onState] `(listening)` when it starts or stops.
   * @param {function} [options.onError] `(message)`.
   * @param {string} [options.lang] A BCP 47 tag; the page's language otherwise.
   */
  constructor({ onFinal, onInterim = null, onState = null, onError = null, lang = "" } = {}) {
    this.onFinal = onFinal;
    this.onInterim = onInterim;
    this.onState = onState;
    this.onError = onError;
    this.lang = lang;
    this.listening = false;
    this._recogniser = null;
  }

  start() {
    if (!SpeechInput.supported || this.listening) return false;
    const Recogniser = globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition;
    const recogniser = new Recogniser();
    recogniser.lang = this.lang || (globalThis.document && document.documentElement.lang) || "en-US";
    recogniser.interimResults = true;
    recogniser.maxAlternatives = 3;
    recogniser.continuous = false;
    recogniser.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) {
          const all = Array.from(result).map((alt) => alt.transcript.trim());
          if (this.onFinal && all[0]) this.onFinal(all[0], all.slice(1));
        } else {
          interim += result[0].transcript;
        }
      }
      if (interim && this.onInterim) this.onInterim(interim);
    };
    recogniser.onerror = (event) => {
      if (this.onError && event.error !== "no-speech" && event.error !== "aborted") {
        this.onError(event.error === "not-allowed"
          ? "the microphone is blocked for this page" : `speech: ${event.error}`);
      }
    };
    recogniser.onend = () => this._set(false);
    this._recogniser = recogniser;
    recogniser.start();
    this._set(true);
    return true;
  }

  stop() {
    if (this._recogniser) this._recogniser.stop();
  }

  toggle() {
    return this.listening ? (this.stop(), false) : this.start();
  }

  _set(listening) {
    this.listening = listening;
    if (this.onState) this.onState(listening);
  }
}

/**
 * A chat window: messages, the before/after card, suggested actions, a
 * text box and a microphone. Plain DOM, class names prefixed `fai-` to style,
 * and optional - a React app can drive {@link BotChat} and draw its own.
 */
export class ChatWidget {
  /**
   * @param {HTMLElement} root Where to draw.
   * @param {BotChat} chat
   * @param {object} [options]
   * @param {string} [options.greeting] The first bot line, shown before any turn.
   * @param {string} [options.placeholder]
   * @param {boolean} [options.speech] Offer the microphone when supported.
   * @param {boolean} [options.speakReplies] Read replies aloud after speech input.
   * @param {string} [options.title] A heading over the chat; with `resettable`
   *   it carries the reset button.
   * @param {boolean} [options.resettable] Offer a "New chat" button.
   * @param {function} [options.onReset] Called after the chat was reset.
   */
  constructor(root, chat, { greeting = "Hi! What can I help you with?",
                            placeholder = "Type a message", speech = true,
                            speakReplies = true, title = "",
                            resettable = true, onReset = null } = {}) {
    this.root = root;
    this.chat = chat;
    this.speakReplies = speakReplies;
    this.greeting = greeting;
    this.onReset = onReset;
    this._lastVia = "typed";
    const outerReply = chat.onReply;
    chat.onReply = (reply, input) => {
      this._draw(reply, input);
      if (outerReply) outerReply(reply, input);
    };
    const outerError = chat.onError;
    chat.onError = (error, input) => {
      this._line("bot", error.message || String(error), "fai-error");
      if (outerError) outerError(error, input);
    };

    root.classList.add("fai-chat");
    root.innerHTML = "";
    if (title || resettable) {
      const head = el("div", "fai-head");
      head.append(el("span", "fai-title", title));
      if (resettable) {
        const fresh = el("button", "fai-reset", "New chat");
        fresh.type = "button";
        fresh.title = "Forget this conversation and start again";
        fresh.addEventListener("click", () => this.reset());
        head.append(fresh);
      }
      root.append(head);
    }
    this.log = el("div", "fai-log");
    this.log.setAttribute("role", "log");
    this.log.setAttribute("aria-live", "polite");
    this.actions = el("div", "fai-actions");
    const bar = el("form", "fai-bar");
    this.input = el("input", "fai-input");
    this.input.type = "text";
    this.input.placeholder = placeholder;
    this.input.setAttribute("aria-label", "Message");
    this.mic = el("button", "fai-mic");
    this.mic.type = "button";
    this.mic.title = "Speak";
    this.mic.setAttribute("aria-label", "Speak");
    this.mic.textContent = "\u{1F3A4}";
    const go = el("button", "fai-send");
    go.type = "submit";
    go.textContent = "Send";
    bar.append(this.input, this.mic, go);
    root.append(this.log, this.actions, bar);

    bar.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = this.input.value.trim();
      if (!text) return;
      this.input.value = "";
      this._you(text, "typed");
      this.chat.say(text).catch(() => {});
    });

    this.speech = null;
    if (speech && SpeechInput.supported) {
      this.speech = new SpeechInput({
        onInterim: (text) => { this.input.value = text; },
        onFinal: (text, alternatives) => {
          this.input.value = "";
          this._you(text, "speech");
          this.chat.speak(text, alternatives).catch(() => {});
        },
        onState: (on) => this.mic.classList.toggle("is-listening", on),
        onError: (message) => this._line("bot", message, "fai-error"),
      });
      this.mic.addEventListener("click", () => this.speech.toggle());
    } else {
      this.mic.hidden = true;
    }

    if (greeting) this._line("bot", greeting);
  }

  /** Start over: the conversation, the log, the suggestions and the microphone. */
  reset() {
    if (this.speech) this.speech.stop();
    if (typeof globalThis.speechSynthesis !== "undefined") globalThis.speechSynthesis.cancel();
    this.chat.reset();
    this.log.innerHTML = "";
    this.actions.innerHTML = "";
    this._offered = [];
    this.input.value = "";
    if (this.greeting) this._line("bot", this.greeting);
    this.input.focus();
    if (this.onReset) this.onReset(this);
  }

  _you(text, via) {
    this._lastVia = via;
    this._line("you", text, via === "speech" ? "fai-spoken" : "");
  }

  _line(who, text, extra = "") {
    const line = el("div", `fai-msg fai-${who}${extra ? " " + extra : ""}`);
    line.textContent = text;
    this.log.append(line);
    this.log.scrollTop = this.log.scrollHeight;
    return line;
  }

  _draw(reply, input) {
    if (input && input.type === "action") {
      const chosen = (this._offered || []).find((a) => a.id === input.action);
      if (chosen) this._line("you", chosen.label, "fai-clicked");
    }
    const said = (reply.messages || []).map((m) => m.text).join(" ");
    if (said) this._line("bot", said);
    const form = reply.form;
    if (form && Object.keys(form.changes || {}).length
        && ["collecting", "ready"].includes(reply.conversation.status)) {
      this.log.append(changeCard(form));
      this.log.scrollTop = this.log.scrollHeight;
    }
    this._offered = reply.actions || [];
    this.actions.innerHTML = "";
    for (const action of this._offered) {
      const button = el("button", `fai-action fai-${action.type}`
        + (action.style === "primary" ? " is-primary" : ""));
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", () => {
        this.actions.innerHTML = "";
        this.chat.click(action).catch(() => {});
      });
      this.actions.append(button);
    }
    if (said && this.speakReplies && this._lastVia === "speech"
        && typeof globalThis.speechSynthesis !== "undefined") {
      globalThis.speechSynthesis.speak(new SpeechSynthesisUtterance(said));
    }
    this._lastVia = "typed";
  }
}

/** The before/after card: one row per field the conversation changed or needs. */
export function changeCard(form) {
  const card = el("div", "fai-card");
  const table = el("table", "fai-diff");
  const head = el("tr");
  for (const title of ["", "Now", "New"]) head.append(el("th", "", title));
  table.append(head);
  for (const row of form.fields || []) {
    if (!["changed", "missing", "outdated"].includes(row.status)) continue;
    const tr = el("tr", `fai-row fai-${row.status}`);
    tr.append(el("th", "", row.label));
    tr.append(el("td", "fai-before", row.before || "—"));
    const after = el("td", "fai-after", row.after || "?");
    if (row.source === "model") after.title = `suggested (confidence ${row.confidence})`;
    tr.append(after);
    table.append(tr);
  }
  card.append(table);
  return card;
}

function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

function readValue(element) {
  if (element.type === "checkbox") return element.checked ? (element.value || "yes") : "";
  if (element.length !== undefined && element.tagName === undefined) {
    // A radio group arrives as a RadioNodeList.
    return element.value || "";
  }
  return element.value == null ? "" : String(element.value);
}

function writeValue(element, value) {
  if (element.type === "checkbox") {
    element.checked = Boolean(value) && value !== "no" && value !== "false";
    return;
  }
  element.value = value == null ? "" : value;
}

function mark(element, className, on) {
  const target = element.classList ? element : null;
  if (target) target.classList.toggle(className, on);
  if (element.setAttribute) {
    if (on) element.setAttribute("data-fillerai", "suggested");
    else element.removeAttribute("data-fillerai");
  }
}

// So a page that mixes a module with classic scripts can still reach this.
if (typeof globalThis !== "undefined") {
  globalThis.FillerAI = FillerAI;
  globalThis.FillerAIError = FillerAIError;
  globalThis.FillerAIFormBinder = FormBinder;
  globalThis.FillerAIBotChat = BotChat;
  globalThis.FillerAIChatWidget = ChatWidget;
  globalThis.FillerAISpeechInput = SpeechInput;
}

export default FillerAI;
