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
 * The only thing here that is not a thin wrapper around `fetch` is
 * {@link FormBinder}, and that is because binding a model to a real form is
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
}

export default FillerAI;
