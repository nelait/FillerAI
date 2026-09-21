/* FillerAI UI.
 *
 * Plain ES modules-free JavaScript, no build step. The server owns all the
 * logic: every button here is one POST to an endpoint that calls the same
 * functions the CLI does, which is what stops the UI and the CLI drifting.
 */
'use strict';

const state = {
  kind: 'html',        // 'html' | 'spec'
  sourceText: '',      // kept so the simulator can replay the real form
  sourceName: '',      // where it came from, so exports are named usefully
  schema: null,
  summary: null,
  records: [],
  columns: [],
  problems: [],
  semanticTypes: [],
  edited: new Set(),   // field names the user has set by hand
  model: null,         // { model_id, seeds, fields, evaluation, ... }
  typed: {},           // what the imaginary agent has entered on the Train step
  algorithms: [],      // what can be selected, from /api/meta
  algorithm: null,     // the one selected now
  run: null,           // the training run being watched, if any
  // Library ids for what is on screen, so each stage records what it came
  // from instead of a pile of unrelated entries.
  ids: { source: null, schema: null, dataset: null, model: null },
  // Who is signed in, when the server is running with accounts. Null means
  // it is not, which is the single-user tool: no login, one library.
  user: null,
  csrf: '',
};

const PREVIEW_ROWS = 100;
const REVIEW_BELOW = 0.7;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  let response;
  const headers = { 'Content-Type': 'application/json' };
  // The session is in a cookie the browser attaches by itself; this header
  // is the part another origin cannot forge, so it goes on every call.
  if (state.csrf) headers['X-FillerAI-Token'] = state.csrf;
  try {
    response = await fetch(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body || {}),
    });
  } catch (error) {
    throw new Error('the FillerAI server is not reachable - is it still running?');
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(`the server replied with ${response.status}`);
  }
  if (!response.ok) {
    // A session that ran out mid-afternoon is not an error to show in a
    // toast behind a page full of stale buttons: it is a trip to the door.
    if (payload.sign_in || payload.must_change) {
      window.location.href = '/login';
      throw new Error('your session ended - signing in again');
    }
    throw new Error(payload.error || `request failed (${response.status})`);
  }
  return payload;
}

let toastTimer = null;
function toast(message, isError) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('bad', Boolean(isError));
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 3000);
}

async function withBusy(button, label, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    return await task();
  } catch (error) {
    toast(error.message, true);
    return null;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function download(filename, text, mime) {
  const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke on the next tick so the download has certainly started.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ------------------------------------------------------------ navigation

function showPanel(name) {
  document.querySelectorAll('.panel').forEach((panel) => {
    panel.classList.toggle('is-current', panel.id === `panel-${name}`);
  });
  document.querySelectorAll('.step').forEach((step) => {
    const on = step.dataset.panel === name;
    step.classList.toggle('is-current', on);
    step.setAttribute('aria-selected', String(on));
  });
  if (name === 'train') renderTrain();
  if (name === 'simulate') renderSimulate();
  if (name === 'library') loadLibrary();
}

function unlock(name) {
  const step = document.querySelector(`.step[data-panel="${name}"]`);
  if (step) step.disabled = false;
}

document.getElementById('steps').addEventListener('click', (event) => {
  const step = event.target.closest('.step');
  if (step && !step.disabled) showPanel(step.dataset.panel);
});

// ---------------------------------------------------------------- source

document.querySelectorAll('.seg-btn').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.seg-btn').forEach((b) => b.classList.remove('is-on'));
    button.classList.add('is-on');
    state.kind = button.dataset.kind;
    $('source').placeholder = state.kind === 'html'
      ? 'Paste an HTML form here, or drop a .html file onto this box.'
      : 'Paste a field spec, or a schema you saved earlier.';
  });
});

function loadText(text, kind, name) {
  state.sourceName = name ? name.replace(/\.(html?|fields\.json|json)$/i, '') : '';
  if (kind) {
    state.kind = kind;
    document.querySelectorAll('.seg-btn').forEach((b) => {
      b.classList.toggle('is-on', b.dataset.kind === kind);
    });
  }
  $('source').value = text;
}

$('file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const text = await file.text();
  loadText(text, /\.json$/i.test(file.name) ? 'spec' : 'html', file.name);
  toast(`loaded ${file.name}`);
  event.target.value = '';
});

const drop = $('drop');
['dragenter', 'dragover'].forEach((type) => {
  drop.addEventListener(type, (event) => {
    event.preventDefault();
    drop.classList.add('is-over');
  });
});
['dragleave', 'drop'].forEach((type) => {
  drop.addEventListener(type, (event) => {
    event.preventDefault();
    if (type === 'dragleave' && drop.contains(event.relatedTarget)) return;
    drop.classList.remove('is-over');
  });
});
drop.addEventListener('drop', async (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const text = await file.text();
  loadText(text, /\.json$/i.test(file.name) ? 'spec' : 'html', file.name);
  toast(`loaded ${file.name}`);
});

$('clear').addEventListener('click', () => {
  $('source').value = '';
  state.sourceName = '';
});

$('extract').addEventListener('click', () => withBusy($('extract'), 'Reading...', async () => {
  const content = $('source').value;
  const result = await api('/api/extract', {
    kind: state.kind, content, name: state.sourceName || 'form',
  });
  state.sourceText = content;
  state.schema = result.schema;
  state.summary = result.summary;
  state.edited = new Set();
  state.records = [];
  state.problems = [];
  state.ids = {
    source: result.source_id || null,
    schema: result.schema_id || null,
    dataset: null,
    model: null,
  };
  renderSchema();
  unlock('schema');
  unlock('generate');
  showPanel('schema');
  const review = result.summary.needs_review.length;
  toast(review
    ? `${result.summary.fields} fields, ${review} worth a look`
    : `${result.summary.fields} fields, all understood`);
}));

// ---------------------------------------------------------------- schema

function constraintText(field) {
  const c = field.constraints || {};
  const parts = [];
  if (c.required) parts.push('required');
  if (c.max_length != null) parts.push(`max ${c.max_length}`);
  if (c.min_length != null) parts.push(`min ${c.min_length}`);
  if (c.minimum != null || c.maximum != null) {
    parts.push(`${c.minimum ?? '-'}..${c.maximum ?? '-'}`);
  }
  if (c.step != null) parts.push(`step ${c.step}`);
  if (c.pattern) parts.push('pattern');
  if (field.options && field.options.length) parts.push(`${field.options.length} options`);
  return parts.join(' · ');
}

function renderStats() {
  const summary = state.summary || { fields: 0, screens: 0, groups: [], needs_review: [] };
  const review = summary.needs_review.length;
  $('stats').innerHTML = `
    <div class="stat"><b>${summary.fields}</b><span>fields</span></div>
    <div class="stat"><b>${summary.screens || 1}</b><span>screens</span></div>
    <div class="stat"><b>${summary.groups.length}</b><span>groups</span></div>
    <div class="stat ${review ? 'warn' : 'good'}">
      <b>${review}</b><span>${review === 1 ? 'to review' : 'to review'}</span>
    </div>`;
}

function renderSchema() {
  if (!state.schema) return;
  renderStats();

  const needle = $('filter').value.trim().toLowerCase();
  const onlyReview = $('onlyReview').checked;
  const options = state.semanticTypes
    .map((t) => `<option value="${t}">${t}</option>`).join('');

  const rows = state.schema.fields.filter((field) => {
    if (onlyReview && field.confidence >= REVIEW_BELOW) return false;
    if (!needle) return true;
    return `${field.name} ${field.label || ''} ${field.semantic_type}`
      .toLowerCase().includes(needle);
  });

  $('schemaBody').innerHTML = rows.map((field) => {
    const edited = state.edited.has(field.name);
    const review = field.confidence < REVIEW_BELOW;
    const pill = edited ? 'set' : (review ? 'warn' : 'good');
    const shown = edited ? 'set' : field.confidence.toFixed(2);
    const title = (field.evidence || []).join('\n').replace(/"/g, '&quot;');
    return `
      <tr class="${review && !edited ? 'is-review' : ''} ${edited ? 'is-edited' : ''}"
          data-name="${encodeURIComponent(field.name)}">
        <td class="name">${escapeHtml(field.name)}</td>
        <td>${field.label ? escapeHtml(field.label) : '<span class="empty">-</span>'}</td>
        <td>
          <select data-edit="semantic_type">
            ${options.replace(`value="${field.semantic_type}"`,
                              `value="${field.semantic_type}" selected`)}
          </select>
        </td>
        <td><input type="text" data-edit="group" value="${escapeAttr(field.group || '')}"
                   placeholder="none"></td>
        <td class="cons">${constraintText(field) || '<span class="empty">-</span>'}</td>
        <td><span class="pill ${pill}" title="${title}">${shown}</span></td>
      </tr>`;
  }).join('');

  if (!rows.length) {
    $('schemaBody').innerHTML =
      '<tr><td colspan="6" class="muted" style="padding:18px">No fields match.</td></tr>';
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}
function escapeAttr(text) {
  return escapeHtml(text).replace(/"/g, '&quot;');
}

// Edits are applied to the schema and marked authoritative, so re-running
// inference will not quietly undo the user's correction.
$('schemaBody').addEventListener('change', (event) => {
  const control = event.target.closest('[data-edit]');
  if (!control) return;
  const name = decodeURIComponent(control.closest('tr').dataset.name);
  const field = state.schema.fields.find((f) => f.name === name);
  if (!field) return;

  const key = control.dataset.edit;
  if (key === 'semantic_type') {
    field.semantic_type = control.value;
    field.confidence = 1.0;
    field.evidence = ['set by hand in the UI'];
    state.edited.add(name);
  } else if (key === 'group') {
    field.group = control.value.trim() || null;
  }
  renderSchema();
});

$('filter').addEventListener('input', renderSchema);
$('onlyReview').addEventListener('change', renderSchema);

$('downloadSchema').addEventListener('click', () => {
  if (!state.schema) return;
  download(`${state.schema.name || 'form'}.schema.json`,
           JSON.stringify(state.schema, null, 2), 'application/json');
});

$('toGenerate').addEventListener('click', () => showPanel('generate'));

// -------------------------------------------------------------- generate

$('blankRate').addEventListener('input', (event) => {
  $('blankOut').textContent = Number(event.target.value).toFixed(2);
});

$('run').addEventListener('click', () => withBusy($('run'), 'Generating...', async () => {
  if (!state.schema) { toast('read a form first', true); return; }
  const result = await api('/api/generate', {
    schema: state.schema,
    schema_id: state.ids.schema,
    count: Number($('count').value),
    seed: $('seed').value,
    blank_rate: Number($('blankRate').value),
    safe_identifiers: $('safeIds').checked,
    check: true,
  });
  state.records = result.records;
  state.columns = result.columns;
  state.problems = result.problems;
  state.ids.dataset = result.dataset_id || null;
  if (result.schema_id) state.ids.schema = result.schema_id;
  // A new dataset makes the old model stale: clear it rather than leave a
  // table on screen that describes records that no longer exist.
  state.model = null;
  state.ids.model = null;
  state.typed = {};
  ['tryIt', 'trainReport', 'trainStats', 'voterPanel', 'treePanel', 'runPanel']
    .forEach((id) => { $(id).hidden = true; });
  $('downloadModel').hidden = true;
  unlock('train');
  renderData();
  toast(`${result.count} records generated`);
}));

function renderData() {
  const status = $('genStatus');
  status.hidden = false;
  if (state.problems.length) {
    const shown = state.problems.slice(0, 12).map((p) => `<li>${escapeHtml(p)}</li>`).join('');
    const more = state.problems.length > 12
      ? `<li>... and ${state.problems.length - 12} more</li>` : '';
    status.className = 'status bad';
    status.innerHTML =
      `<strong>${state.problems.length} problem(s) found.</strong><ul>${shown}${more}</ul>`;
  } else {
    status.className = 'status ok';
    status.textContent =
      `Checked ${state.records.length} records: values fit the form, and each record is coherent.`;
  }

  $('dataHead').innerHTML =
    `<tr>${state.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr>`;

  const rows = state.records.slice(0, PREVIEW_ROWS);
  $('dataBody').innerHTML = rows.map((record) => `<tr>${
    state.columns.map((column) => {
      const value = record[column];
      if (value === '' || value === null || value === undefined) {
        return '<td class="empty">-</td>';
      }
      if (typeof value === 'boolean') return `<td>${value ? 'yes' : 'no'}</td>`;
      return `<td>${escapeHtml(value)}</td>`;
    }).join('')
  }</tr>`).join('');

  const hidden = state.records.length - rows.length;
  $('truncated').hidden = hidden <= 0;
  $('truncated').textContent = hidden > 0
    ? `Showing the first ${PREVIEW_ROWS} of ${state.records.length}. Export to get them all.`
    : '';

  $('exportBar').hidden = false;
  $('resultCount').textContent =
    `${state.records.length} records, ${state.columns.length} columns`;
}

document.querySelectorAll('[data-export]').forEach((button) => {
  button.addEventListener('click', () => withBusy(button, '...', async () => {
    if (!state.records.length) { toast('generate some records first', true); return; }
    const format = button.dataset.export;
    const result = await api('/api/export', {
      schema: state.schema, records: state.records, format,
    });
    const mime = format === 'csv' ? 'text/csv' : 'application/json';
    download(result.filename, result.text, `${mime};charset=utf-8`);
  }));
});

// ----------------------------------------------------------------- train
//
// The server holds the trained model and answers /api/predict as the user
// types; the panel only draws. Keeping it that way is what stops the UI and
// the CLI disagreeing about what the model thinks.

function fieldByName(name) {
  return (state.schema && state.schema.fields.find((f) => f.name === name)) || null;
}

function confidenceClass(confidence, threshold) {
  if (confidence >= 0.9) return 'good';
  if (confidence >= (threshold == null ? currentThreshold() : threshold)) return 'set';
  return 'warn';
}

function currentThreshold() {
  return Number($('thresh').value);
}

$('thresh').addEventListener('input', (event) => {
  $('threshOut').textContent = Number(event.target.value).toFixed(2);
  if (state.model) refillPreview();
});

// -- choosing an algorithm ---------------------------------------------------
//
// The picker, its settings and the recipe beside it all come from the server:
// adding an algorithm there puts it in this list with its own knobs and its
// own account of what it does, and nothing here changes.

function setAlgorithms(list, preferred) {
  state.algorithms = list || [];
  const select = $('algorithm');
  select.innerHTML = state.algorithms
    .map((a) => `<option value="${escapeAttr(a.name)}">${escapeHtml(a.label)}</option>`)
    .join('');
  select.value = preferred || (state.algorithms[0] || {}).name || '';
  renderAlgorithm();
}

function currentAlgorithm() {
  return state.algorithms.find((a) => a.name === $('algorithm').value) || null;
}

// A float knob needs a step it can actually express. A flat 0.05 was fine
// until an engine arrived with a knob whose default is 0.00002: the browser
// then calls the field invalid on arrival, and its arrows step straight past
// the top of the range. So take the finer of a twentieth of the range and the
// default's own size, and round that down to a power of ten, which is the
// digit a person would type.
function knobStep(knob) {
  if (knob.kind === 'int') return '1';
  const span = Math.abs(Number(knob.high) - Number(knob.low));
  const fine = Math.abs(Number(knob.default)) || span / 20;
  const step = Math.min(span / 20 || fine, fine);
  if (!(step > 0) || !isFinite(step)) return 'any';
  return String(Number(Math.pow(10, Math.floor(Math.log10(step))).toPrecision(15)));
}

function renderAlgorithm() {
  const algorithm = currentAlgorithm();
  state.algorithm = algorithm;
  if (!algorithm) return;
  $('algoBlurb').textContent = algorithm.blurb;
  $('algoRecipe').innerHTML = algorithm.recipe
    .map((line) => `<li>${escapeHtml(line)}</li>`).join('');
  $('algoKnobs').innerHTML = algorithm.knobs.map((knob) => `
    <label class="field">
      <span>${escapeHtml(knob.label)}</span>
      <input type="number" data-knob="${escapeAttr(knob.key)}"
             value="${escapeAttr(String(knob.default))}"
             min="${escapeAttr(String(knob.low))}" max="${escapeAttr(String(knob.high))}"
             step="${escapeAttr(knobStep(knob))}">
    </label>`).join('');
}

$('algorithm').addEventListener('change', renderAlgorithm);

function tuning() {
  const out = {};
  document.querySelectorAll('#algoKnobs [data-knob]').forEach((input) => {
    if (String(input.value).trim() !== '') out[input.dataset.knob] = Number(input.value);
  });
  return out;
}

function trainRequest() {
  return {
    schema: state.schema,
    records: state.records,
    algorithm: $('algorithm').value,
    tuning: tuning(),
    use_rules: $('useRules').checked,
    ask: Number($('askCount').value),
    seed: $('seed').value,
    schema_id: state.ids.schema,
    dataset_id: state.ids.dataset,
  };
}

$('showScript').addEventListener('click', () => withBusy($('showScript'), '...', async () => {
  const result = await api('/api/train/script', trainRequest());
  showRun();
  $('runStage').textContent = `${result.algorithm.label}: the script`;
  $('runLog').textContent = `# as one command\n${result.command}\n\n${result.script}`;
  $('runBar').style.width = '0%';
  $('runClock').textContent = 'not run yet';
}));

// -- running it --------------------------------------------------------------

function showRun() {
  $('runPanel').hidden = false;
  $('runLog').hidden = false;
  $('runToggle').textContent = 'Hide log';
}

$('runToggle').addEventListener('click', () => {
  const log = $('runLog');
  log.hidden = !log.hidden;
  $('runToggle').textContent = log.hidden ? 'Show log' : 'Hide log';
});

const LEVEL_MARK = { step: '==', warn: '!!', done: 'ok' };

function appendLog(lines) {
  const log = $('runLog');
  const stuck = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  log.textContent += lines.map((line) =>
    `[${line.at.toFixed(2).padStart(6)}s] ${LEVEL_MARK[line.level] || '  '} ${line.text}\n`
  ).join('');
  // Follow the tail only if the reader is already at it: scrolling back to
  // read a line and being yanked forward again is the thing a live log most
  // often gets wrong.
  if (stuck) log.scrollTop = log.scrollHeight;
  const last = lines.filter((line) => line.level === 'step').pop();
  if (last) $('runStage').textContent = last.text;
}

$('trainRun').addEventListener('click', () => withBusy($('trainRun'), 'Training...', async () => {
  if (!state.schema) { toast('read a form first', true); return; }
  if (!state.records.length) { toast('generate some records to learn from first', true); return; }

  const started = await api('/api/train/start', trainRequest());
  state.run = started.run_id;
  showRun();
  $('runLog').textContent = '';
  $('runStage').textContent = `${started.algorithm.label}: starting`;
  $('runBar').style.width = '2%';
  ['tryIt', 'trainReport', 'trainStats', 'voterPanel', 'treePanel']
    .forEach((id) => { $(id).hidden = true; });
  await watchRun(started);
}));

async function watchRun(started) {
  let cursor = 0;
  for (;;) {
    const log = await api('/api/train/log', { run_id: started.run_id, cursor });
    cursor = log.cursor;
    if (log.lines.length) appendLog(log.lines);
    const progress = log.progress || {};
    $('runBar').style.width =
      `${Math.max(2, Math.round((progress.fraction || 0) * 100))}%`;
    $('runClock').textContent = `${(progress.elapsed || 0).toFixed(1)}s`;

    if (log.error) {
      $('runStage').textContent = 'Training failed';
      toast(log.error, true);
      return;
    }
    if (log.result) {
      state.model = log.result;
      state.ids.model = log.result.model_entry_id || null;
      if (log.result.dataset_id) state.ids.dataset = log.result.dataset_id;
      state.typed = {};
      $('runBar').style.width = '100%';
      $('runStage').textContent = 'Done';
      // The script goes under the log it produced, so the run and the way to
      // repeat it are in one place.
      $('runLog').textContent +=
        `\n# repeat this run\n${started.command}\n\n${started.script}`;
      renderTrainResult();
      await refillPreview();
      toast(`learned from ${log.result.trained_on} records`);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

$('downloadModel').addEventListener('click', () => withBusy($('downloadModel'), '...', async () => {
  if (!state.model) return;
  const result = await api('/api/model', { model_id: state.model.model_id });
  download(result.filename, result.text, 'application/json;charset=utf-8');
}));

function renderTrain() {
  // Reached by clicking the step. Everything below needs a model, so until
  // there is one the panel is just its controls and a nudge.
  const status = $('trainStatus');
  if (state.model) return;
  status.hidden = false;
  status.className = 'status';
  status.textContent = state.records.length
    ? `Ready: ${state.records.length} records to learn from.`
    : 'Generate some records on the previous step, then train on them here.';
}

function renderTrainResult() {
  const model = state.model;
  const evaluation = model.evaluation;

  const status = $('trainStatus');
  status.hidden = false;
  status.className = 'status ok';
  const algorithm = (state.algorithms.find((a) => a.name === model.algorithm) || {}).label
    || model.algorithm || 'The model';
  if (evaluation) {
    status.textContent = `${algorithm}: learned from ${model.trained_on} records. `
      + `On ${model.held_out} it never saw: ${evaluation.headline}.`;
  } else if (model.reopened) {
    // Reopened from the library. Its accuracy was measured during a run that
    // is over, against records held back then; showing that number here as
    // though it belonged to this moment would be the wrong kind of tidy.
    status.textContent = `${algorithm}: learned from ${model.trained_on} records, `
      + `reopened from the library. Train again to measure it afresh.`;
  } else {
    status.textContent = `${algorithm}: learned from ${model.trained_on} records. `
      + `Too few to hold any back, so there is no measured accuracy yet - `
      + `generate more for that.`;
  }

  const engine = Object.entries(model.engine || {})
    .map(([key, value]) => `<div class="stat small"><b>${escapeHtml(String(value))}</b>`
                         + `<span>${escapeHtml(key)}</span></div>`).join('');
  $('trainStats').hidden = false;
  $('trainStats').innerHTML = `
    <div class="stat"><b>${model.counts.predictable}</b><span>the model can fill</span></div>
    <div class="stat"><b>${model.counts.yours}</b><span>yours to type</span></div>
    <div class="stat"><b>${model.rules.length}</b><span>rules found</span></div>
    <div class="stat ${evaluation && evaluation.accepted_accuracy >= 0.9 ? 'good' : 'warn'}">
      <b>${evaluation ? Math.round(evaluation.accepted_accuracy * 100) + '%' : '-'}</b>
      <span>right when it fills</span>
    </div>${engine}`;

  $('downloadModel').hidden = false;
  renderVoteWeights();
  renderSeedInputs();
  renderTrees();
  renderReport();
}

// -- the fitted vote weights -------------------------------------------------
//
// Several engines can have an opinion about one field, and every opinion used
// to be believed by a hand-picked formula. Now the formula is fitted, and this
// panel is where that shows: what each thing known about a vote does to how
// loudly it speaks, and the two numbers that decided whether the fitted
// weights were kept at all.
//
// The wording lives here rather than on the server because it is wording. What
// arrives is five features and five numbers.

const VOTER_FEATURES = {
  heuristic: 'what the engine asked for',
  strength: 'how well that predictor scores',
  support: 'how many records back it',
  peak: 'how decisive the vote is',
  floor: 'it is only the usual answer',
};

function renderVoteWeights() {
  const panel = $('voterPanel');
  const combiner = (state.model && state.model.combiner) || {};
  const votes = combiner.votes || 0;

  // Nothing was measured: too few records to spare any, and the hand-picked
  // weights stand unexamined. Saying so here would be a sentence about an
  // absence, so the panel is simply not there.
  if (!votes) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const loss = combiner.loss || 0;
  const baseLoss = combiner.baseline_loss || 0;
  const better = baseLoss > 0 ? (baseLoss - loss) / baseLoss : 0;
  const verdict = $('voterVerdict');

  if (combiner.fitted) {
    verdict.className = 'pill good';
    verdict.textContent = `kept: ${(better * 100).toFixed(1)}% better`;
    $('voterNote').textContent =
      `Fitted on ${votes.toLocaleString()} votes from records held back from the`
      + ` rest of the run, then checked against the hand-picked weights on`
      + ` records neither had seen: ${loss.toFixed(4)} against ${baseLoss.toFixed(4)}`
      + ` (lower is better), filling ${(combiner.accuracy * 100).toFixed(1)}% right`
      + ` against ${(combiner.baseline_accuracy * 100).toFixed(1)}%.`;
  } else {
    // Measured and beaten by the guess. Worth showing: it is the answer to
    // "did fitting this help", and the answer was no on these records.
    verdict.className = 'pill warn';
    verdict.textContent = 'measured, then declined';
    $('voterNote').textContent =
      `Fitted on ${votes.toLocaleString()} votes, then checked against the`
      + ` hand-picked weights on records neither had seen: ${loss.toFixed(4)}`
      + ` against ${baseLoss.toFixed(4)} (lower is better). The hand-picked ones`
      + ` were at least as good, so they stay and this model behaves exactly as`
      + ` it would have without them.`;
    $('voterPulls').innerHTML = '';
    return;
  }

  const features = combiner.features || [];
  const weights = combiner.weights || [];
  const widest = Math.max(0.01, ...weights.map((w) => Math.abs(w)));

  // Largest pull first, which is the order the question is asked in: what
  // matters most to how loudly a vote speaks?
  const rows = features
    .map((name, i) => [name, weights[i] || 0])
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));

  $('voterPulls').innerHTML = rows.map(([name, weight]) => {
    const share = (Math.abs(weight) / widest) * 50;
    const bar = weight >= 0
      ? `<i style="left: 50%; width: ${share.toFixed(1)}%"></i>`
      : `<i class="down" style="right: 50%; width: ${share.toFixed(1)}%"></i>`;
    return `<li>
      <span class="who">${escapeHtml(VOTER_FEATURES[name] || name)}</span>
      <span class="track">${bar}</span>
      <span class="how">${weight >= 0 ? '+' : ''}${weight.toFixed(2)}</span>
    </li>`;
  }).join('');
}

// -- the drawn tree ----------------------------------------------------------
//
// Only the engines that grow one offer this, and the panel simply is not
// there for the others rather than being there and empty.

function renderTrees() {
  const drawable = (state.model && state.model.drawable) || [];
  $('treePanel').hidden = !drawable.length;
  if (!drawable.length) return;
  $('treeField').innerHTML = drawable
    .map((name) => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`)
    .join('');
  drawTree();
}

async function drawTree() {
  if (!state.model) return;
  const field = $('treeField').value;
  if (!field) return;
  try {
    const result = await api('/api/train/tree',
                             { model_id: state.model.model_id, field });
    $('treeDraw').textContent = result.lines.length
      ? result.lines.join('\n')
      : `nothing was grown for ${field}`;
  } catch (error) {
    $('treeDraw').textContent = error.message;
  }
}

$('treeField').addEventListener('change', drawTree);

function renderSeedInputs() {
  const model = state.model;
  $('tryIt').hidden = false;
  $('seedInputs').innerHTML = model.seeds.map((name) => {
    const field = fieldByName(name);
    // Dense forms repeat their labels - three boxes all called "City" is
    // normal - so a label shared with another seed gets its group back.
    let text = (field && field.label) || name;
    const clashes = model.seeds.some((other) => {
      const f = other !== name && fieldByName(other);
      return f && (f.label || other) === text;
    });
    if (clashes && field && field.group) text = `${text} (${field.group})`;
    const label = escapeHtml(text);
    const options = field && field.options && field.options.length
      ? `<select data-seed="${escapeAttr(name)}">
           <option value="">-</option>
           ${field.options.map((o) => `<option value="${escapeAttr(o.value)}">${escapeHtml(o.label || o.value)}</option>`).join('')}
         </select>`
      : `<input type="text" data-seed="${escapeAttr(name)}"
                placeholder="${escapeAttr(exampleFor(name))}">`;
    return `<label class="field"><span>${label}</span>${options}</label>`;
  }).join('') || '<span class="muted">Nothing in this form unlocks anything else.</span>';
}

function exampleFor(name) {
  const row = (state.model.fields || []).find((f) => f.name === name);
  return (row && row.examples && row.examples[0]) || '';
}

let fillTimer = null;
$('seedInputs').addEventListener('input', (event) => {
  const control = event.target.closest('[data-seed]');
  if (!control) return;
  state.typed[control.dataset.seed] = control.value;
  // Typing is faster than a round trip, so only the last keystroke of a
  // burst asks the server.
  clearTimeout(fillTimer);
  fillTimer = setTimeout(refillPreview, 180);
});
$('seedInputs').addEventListener('change', (event) => {
  const control = event.target.closest('[data-seed]');
  if (!control) return;
  state.typed[control.dataset.seed] = control.value;
  refillPreview();
});

async function refillPreview() {
  if (!state.model) return;
  const observed = {};
  Object.entries(state.typed).forEach(([name, value]) => {
    if (String(value).trim()) observed[name] = String(value).trim();
  });

  let result;
  try {
    result = await api('/api/predict', {
      model_id: state.model.model_id,
      observed,
      threshold: currentThreshold(),
    });
  } catch (error) {
    // The server restarted, or the model fell out of its small cache. Say
    // what to do rather than leaving a stale table on screen.
    $('fillBody').innerHTML =
      `<tr><td colspan="4" class="muted" style="padding:18px">${escapeHtml(error.message)}</td></tr>`;
    return;
  }

  const byName = new Map(result.predictions.map((p) => [p.field, p]));
  const threshold = result.threshold;

  $('fillBody').innerHTML = state.schema.fields.map((field) => {
    const name = field.name;
    if (name in result.given) {
      return row(name, escapeHtml(result.given[name]), '<span class="pill set">typed</span>', 'you entered this');
    }
    const prediction = byName.get(name);
    if (!prediction) return '';
    if (prediction.value === null || prediction.value === undefined) {
      const why = (prediction.because || [])[0] || 'nothing learned about this field';
      return row(name, '<span class="empty">-</span>',
                 '<span class="pill warn">yours</span>', escapeHtml(why));
    }
    const confident = prediction.confidence >= threshold;
    const pill = `<span class="pill ${confidenceClass(prediction.confidence)}">${prediction.confidence.toFixed(2)}</span>`;
    const value = confident
      ? escapeHtml(prediction.value)
      : `<span class="empty">${escapeHtml(prediction.value)}</span>`;
    return row(name, value, pill, escapeHtml((prediction.because || []).join('; ')));
  }).join('');

  $('trainStatus').textContent =
    `${Object.keys(observed).length} typed, ${result.offered} filled at ${threshold.toFixed(2)} or better.`;
  $('trainStatus').className = 'status ok';
}

function row(name, value, pill, why) {
  return `<tr><td class="name">${escapeHtml(name)}</td><td>${value}</td>`
       + `<td>${pill}</td><td class="cons">${why}</td></tr>`;
}

const ANSWERED_BY = {
  rule: 'a rule', learned: 'other fields', usual: 'its usual value', you: 'you',
};

function renderReport() {
  $('trainReport').hidden = false;
  $('reportBody').innerHTML = state.model.fields.map((row) => {
    const sources = (row.from || []).slice(0, 3).join(', ');
    const detail = row.how === 'you'
      ? (row.format ? `shaped like ${escapeHtml(row.format)}` : 'different in every record')
      : escapeHtml(row.detail || sources);
    const strength = row.how === 'you' ? '<span class="empty">-</span>'
      : `<span class="pill ${row.strength >= 0.7 ? 'good' : 'warn'}">${row.strength.toFixed(2)}</span>`;
    return `<tr>
      <td class="name">${escapeHtml(row.name)}</td>
      <td>${ANSWERED_BY[row.how]}</td>
      <td class="cons">${detail}</td>
      <td>${strength}</td>
      <td class="cons">${(row.examples || []).slice(0, 2).map(escapeHtml).join(', ')}</td>
    </tr>`;
  }).join('');
}

// -------------------------------------------------------------- simulate
//
// The form is drawn once from the layout the server sends, and then only its
// values and badges are updated as the model answers. Re-rendering the whole
// form on every keystroke would be simpler and would also throw away the
// caret the user is typing into, which is the one thing a form must not do.

const sim = {
  layout: null,     // pages of controls, from /api/simulate/form
  seeds: [],        // the fields the model would like typed first
  case: null,       // the record this form is really about, for marking
  caseSeed: null,
  typed: {},        // what the user has entered
  cells: new Map(), // last board, by field name
  modelId: null,    // the model the form on screen was drawn for
  page: 0,
  sweep: null,
};

function simThreshold() {
  return Number($('simThresh').value);
}

$('simThresh').addEventListener('input', (event) => {
  $('simThreshOut').textContent = Number(event.target.value).toFixed(2);
  if (sim.layout) simFill();
});

$('simMarks').addEventListener('change', () => applyBoard());

async function renderSimulate() {
  const status = $('simStatus');
  if (!state.model) {
    status.hidden = false;
    status.className = 'status';
    status.textContent = state.records.length
      ? 'Train a model on the previous step, then bring it here to fill a form.'
      : 'Read a form, generate records, and train a model first. This stage plays that model against a form it has never seen.';
    $('simControls').hidden = true;
    $('simBody').hidden = true;
    return;
  }
  // Same model as the form already on screen: leave the run as it is, so
  // stepping away to another panel and back does not wipe the board.
  if (sim.modelId === state.model.model_id && sim.layout) return;

  status.hidden = false;
  status.className = 'status';
  status.textContent = 'Drawing the form...';
  try {
    const form = await api('/api/simulate/form', { model_id: state.model.model_id });
    sim.layout = form.layout;
    sim.seeds = form.seeds;
    sim.modelId = state.model.model_id;
    sim.page = 0;
    sim.sweep = null;
    $('simSweepOut').hidden = true;
    $('simThresh').value = form.threshold;
    $('simThreshOut').textContent = Number(form.threshold).toFixed(2);
    $('simAssumptions').innerHTML =
      form.assumptions.map((line) => `<li>${escapeHtml(line)}</li>`).join('');
    renderSimModel(form.model);
    $('simControls').hidden = false;
    $('simBody').hidden = false;
    drawPages();
    drawAsks();
    drawForm();
    await newCase();
  } catch (error) {
    status.className = 'status bad';
    status.textContent = error.message;
    $('simControls').hidden = true;
    $('simBody').hidden = true;
  }
}

// -- which model is doing the filling ------------------------------------
//
// A simulation nobody can attribute to a model is a demo. The panel holds a
// cache handle, which says nothing a person would want to read, so the server
// sends what the model is along with the form to draw - and, when the model
// was kept, the library entries it descends from: the page it was read out
// of, the schema, the records. The same chain the library draws, at the point
// where the model is actually being used.

function renderSimModel(info) {
  const note = $('simModelNote');
  const lead = $('simChainLead');
  if (!info) {
    // An older server, or a call that did not carry it. Better an absent card
    // than a card asserting something about a model it was not told.
    $('simModelCard').hidden = true;
    return;
  }
  $('simModelCard').hidden = false;

  const saved = info.entry;
  const title = saved ? saved.name : `${info.form}: ${info.algorithm}`;
  $('simModelName').innerHTML =
    `<b>${escapeHtml(title)}</b>`
    + `<span class="muted">${escapeHtml(info.algorithm_label || info.algorithm)}</span>`
    + (saved ? `<span class="muted">trained ${escapeHtml(whenText(saved.created))}</span>`
             + `<button class="link" data-reveal="${escapeAttr(saved.id)}">`
             + 'find it in the library</button>' : '');

  // The engine's own numbers go on one line rather than into tiles of their
  // own: this column is narrow, and the four that matter about the model
  // should not be pushed off the screen by a forest's depth.
  const engine = Object.entries(info.engine || {})
    .map(([key, value]) => `${escapeHtml(String(value))} ${escapeHtml(key)}`)
    .join(' · ');
  $('simModelStats').innerHTML = `
    <div class="stat"><b>${info.trained_on}</b><span>records learned from</span></div>
    ${info.held_out ? `<div class="stat"><b>${info.held_out}</b>`
                    + '<span>held back from it</span></div>' : ''}
    <div class="stat"><b>${info.fields}</b><span>fields it answers for</span></div>
    <div class="stat"><b>${info.rules}</b><span>rules found</span></div>`;
  $('simEngine').textContent = engine;
  $('simEngine').hidden = !engine;

  // The chain without its last link: that is this model, and it is the
  // heading above. What is left is what the model was made from.
  const chain = (info.lineage || []).slice(0, -1);
  lead.hidden = !chain.length;
  $('simChain').innerHTML = chain.map((entry) => {
    const detail = Object.entries(entry.meta || {})
      .filter(([, value]) => value !== null && value !== '' && value !== undefined)
      .map(([key, value]) => `${escapeHtml(key)} ${escapeHtml(formatMeta(value))}`)
      .join(' · ');
    return `<li class="chain-step">
      <span class="lib-kind ${escapeAttr(entry.kind)}">${escapeHtml(KIND_WORDS[entry.kind] || entry.kind)}</span>
      <div>
        <button class="chain-name" data-reveal="${escapeAttr(entry.id)}"
                title="Find it in the library">${escapeHtml(entry.name)}</button>
        <span class="muted">${escapeHtml(whenText(entry.created))}${detail ? ' · ' + detail : ''}</span>
      </div>
    </li>`;
  }).join('');

  note.hidden = Boolean(saved);
  if (!saved) {
    note.textContent = 'This model is not in the library, so there is no chain '
      + 'behind it to show - and it lasts only as long as the server is '
      + 'running. Train it again with the library on to keep it.';
  }
}

$('simModelCard').addEventListener('click', (event) => {
  const button = event.target.closest('[data-reveal]');
  if (button) revealInLibrary(button.dataset.reveal);
});

// -- drawing the form ---------------------------------------------------

function pageOf(name) {
  return sim.layout.pages.findIndex(
    (page) => page.sections.some((s) => s.controls.some((c) => c.name === name)));
}

function controlOf(name) {
  for (const page of sim.layout.pages) {
    for (const section of page.sections) {
      for (const control of section.controls) if (control.name === name) return control;
    }
  }
  return null;
}

// Dense forms repeat their labels - three boxes all called "City" is normal -
// so a label shared with another field being named gets its group back.
function labelOf(name, among) {
  const control = controlOf(name);
  if (!control) return name;
  const text = control.label || name;
  const clashes = (among || []).some((other) => {
    const c = other !== name && controlOf(other);
    return c && (c.label || other) === text;
  });
  return clashes && control.group ? `${text} (${control.group})` : text;
}

// The fields worth typing first are wherever the form puts them, which on a
// four-screen form is usually not the screen you are looking at. Saying which
// they are, and getting there in one click, is the difference between a
// simulation you can try and one you have to hunt through.
function drawAsks() {
  const node = $('simAsks');
  if (!sim.seeds.length) {
    node.hidden = false;
    node.textContent = 'Nothing in this form unlocks anything else, so there is no useful place to start typing.';
    return;
  }
  node.hidden = false;
  node.innerHTML = 'Start with '
    + sim.seeds.map((name) => {
        const index = pageOf(name);
        const where = index >= 0 && sim.layout.pages.length > 1
          ? ` on ${escapeHtml(sim.layout.pages[index].title)}` : '';
        return `<button type="button" data-goto="${escapeAttr(name)}">`
             + `${escapeHtml(labelOf(name, sim.seeds))}</button>${where}`;
      }).join(', ')
    + '. The rest of the form answers to those.';
}

$('simAsks').addEventListener('click', (event) => {
  const button = event.target.closest('[data-goto]');
  if (!button) return;
  const index = pageOf(button.dataset.goto);
  if (index < 0) return;
  sim.page = index;
  drawPages();
  drawForm();
  const node = document.querySelector(`[data-sim="${CSS.escape(button.dataset.goto)}"]`);
  if (node) { node.scrollIntoView({ block: 'center' }); node.focus(); }
});

function drawPages() {
  const pages = sim.layout.pages;
  const nav = $('simPages');
  nav.hidden = pages.length < 2;
  if (nav.hidden) return;
  nav.innerHTML = pages.map((page, index) => `
    <button class="page ${index === sim.page ? 'is-current' : ''}" role="tab"
            data-page="${index}" aria-selected="${index === sim.page}">
      ${escapeHtml(page.title)} <span class="count">${page.fields}</span>
    </button>`).join('');
}

$('simPages').addEventListener('click', (event) => {
  const button = event.target.closest('[data-page]');
  if (!button) return;
  sim.page = Number(button.dataset.page);
  drawPages();
  drawForm();
  applyBoard();
});

function controlHtml(control) {
  const name = escapeAttr(control.name);
  const common = `data-sim="${name}" id="sim-f-${name}"`;
  if (control.control === 'select' || control.control === 'multiselect') {
    const multiple = control.control === 'multiselect' ? ' multiple size="3"' : '';
    return `<select ${common}${multiple}>
      ${control.control === 'multiselect' ? '' : '<option value=""></option>'}
      ${(control.options || []).map((o) =>
        `<option value="${escapeAttr(o.value)}">${escapeHtml(o.label || o.value)}</option>`
      ).join('')}
    </select>`;
  }
  if (control.control === 'checkbox') {
    return `<input type="checkbox" ${common}>`;
  }
  if (control.control === 'radio') {
    return `<div class="radios" ${common}>${(control.options || []).map((o) =>
      `<label><input type="radio" name="sim-r-${name}"
        value="${escapeAttr(o.value)}"> ${escapeHtml(o.label || o.value)}</label>`
    ).join('')}</div>`;
  }
  if (control.control === 'textarea') {
    return `<textarea ${common} rows="2"
      placeholder="${escapeAttr(control.placeholder || '')}"></textarea>`;
  }
  const limits = [
    control.max_length != null ? ` maxlength="${control.max_length}"` : '',
    control.minimum != null ? ` min="${control.minimum}"` : '',
    control.maximum != null ? ` max="${control.maximum}"` : '',
    control.step != null ? ` step="${control.step}"` : '',
  ].join('');
  return `<input type="${escapeAttr(control.input_type || 'text')}" ${common}${limits}
    placeholder="${escapeAttr(control.placeholder || '')}">`;
}

function drawForm() {
  const page = sim.layout.pages[sim.page];
  if (!page) { $('simForm').innerHTML = ''; return; }
  $('simForm').innerHTML = page.sections.map((section) => `
    <fieldset class="sim-section">
      <legend>${escapeHtml(section.title)}</legend>
      <div class="sim-fields">
        ${section.controls.map((control) => `
          <div class="sim-field" data-for="${escapeAttr(control.name)}">
            <label for="sim-f-${escapeAttr(control.name)}">
              ${escapeHtml(control.label)}
              ${control.required ? '<span class="req" title="required">*</span>' : ''}
              ${sim.seeds.includes(control.name) ? '<span class="pill set">type this</span>' : ''}
            </label>
            ${controlHtml(control)}
            <p class="sim-note"></p>
          </div>`).join('')}
      </div>
    </fieldset>`).join('');
  applyBoard();
}

// -- reading what the user typed ----------------------------------------

function readControl(node) {
  if (node.tagName === 'SELECT' && node.multiple) {
    return Array.from(node.selectedOptions).map((o) => o.value).join('|');
  }
  if (node.type === 'checkbox') return node.checked ? 'true' : 'false';
  return node.value;
}

function writeControl(node, value) {
  // A radio group is a div of inputs rather than one control, so it is the
  // only case where the thing carrying the field name is not the thing
  // holding the value.
  if (node.classList && node.classList.contains('radios')) {
    node.querySelectorAll('input[type="radio"]').forEach((radio) => {
      radio.checked = radio.value === value;
    });
    return;
  }
  if (node.tagName === 'SELECT' && node.multiple) {
    const wanted = new Set(String(value).split('|'));
    Array.from(node.options).forEach((o) => { o.selected = wanted.has(o.value); });
    return;
  }
  if (node.type === 'checkbox') {
    node.checked = ['true', 'yes', 'on', '1'].includes(String(value).toLowerCase());
    return;
  }
  if (node.tagName === 'SELECT') {
    // A predicted value the form has no option for would silently blank the
    // box, which reads as the model having said nothing. Add it instead.
    const has = Array.from(node.options).some((o) => o.value === value);
    if (!has && value) {
      const extra = document.createElement('option');
      extra.value = value;
      extra.textContent = value;
      node.appendChild(extra);
    }
  }
  node.value = value == null ? '' : value;
}

let simTimer = null;
function onSimInput(event) {
  const node = event.target.closest('[data-sim], [name^="sim-r-"]');
  if (!node) return;
  const wrapper = node.closest('.sim-field');
  if (!wrapper) return;
  const name = wrapper.dataset.for;
  const value = node.type === 'radio'
    ? (wrapper.querySelector('input[type="radio"]:checked') || {}).value || ''
    : readControl(node);
  // A box the user has cleared is not a value of "" to predict from - it is
  // a box they have not filled, which is what the model should be told.
  if (String(value).trim()) sim.typed[name] = String(value).trim();
  else delete sim.typed[name];
  clearTimeout(simTimer);
  simTimer = setTimeout(simFill, 200);
}

$('simForm').addEventListener('input', onSimInput);
$('simForm').addEventListener('change', onSimInput);

// -- running it ---------------------------------------------------------

async function simFill() {
  if (!sim.layout || !state.model) return;
  let result;
  try {
    result = await api('/api/simulate/fill', {
      model_id: state.model.model_id,
      typed: sim.typed,
      case: sim.case,
      threshold: simThreshold(),
    });
  } catch (error) {
    $('simStatus').hidden = false;
    $('simStatus').className = 'status bad';
    $('simStatus').textContent = error.message;
    return;
  }
  sim.cells = new Map(result.cells.map((cell) => [cell.name, cell]));
  applyBoard();
  renderSavings(result);
}

function applyBoard() {
  if (!sim.cells.size) return;
  const marks = $('simMarks').checked && Boolean(sim.case);
  document.querySelectorAll('#simForm .sim-field').forEach((wrapper) => {
    const name = wrapper.dataset.for;
    const cell = sim.cells.get(name);
    const node = wrapper.querySelector('[data-sim]');
    if (!cell || !node) return;

    wrapper.className = `sim-field is-${cell.source}`;
    if (marks && cell.verdict && cell.source === 'filled') {
      wrapper.classList.add(cell.verdict === 'right' ? 'is-right' : 'is-wrong');
    }

    // Never write into the box the caret is in: the user is mid-value, and
    // the model's answer for a half-typed field is not worth the interruption.
    const focused = node.contains(document.activeElement) || node === document.activeElement;
    if (!focused && cell.source !== 'typed') {
      writeControl(node, cell.source === 'filled' ? cell.value : '');
    }
    wrapper.querySelector('.sim-note').innerHTML = noteFor(cell, marks);
  });
}

function noteFor(cell, marks) {
  const why = escapeHtml((cell.because || []).join('; '));
  if (cell.source === 'typed') return '<span class="pill set">typed</span>';

  if (cell.source === 'yours') {
    const shape = cell.format ? ` shaped like <code>${escapeHtml(cell.format)}</code>` : '';
    return `<span class="pill warn">yours</span> <span class="why">${why || 'nothing learned about this field'}${shape}</span>`;
  }

  const pill = `<span class="pill ${confidenceClass(cell.confidence, simThreshold())}">`
             + `${cell.confidence.toFixed(2)}</span>`;
  // A held-back value was never put in the box, so it was never right or
  // wrong - it is what moving the slider would have bought or cost.
  const held = cell.source === 'suggested';
  let mark = '';
  if (marks && cell.verdict === 'right') {
    mark = `<span class="verdict ok">${held ? 'would have been right' : 'right'}</span>`;
  }
  if (marks && cell.verdict === 'wrong') {
    mark = held
      ? '<span class="verdict no">would have been wrong</span>'
      : `<span class="verdict no">wrong, the form says ${escapeHtml(cell.truth || '(blank)')}</span>`;
  }
  if (held) {
    return `${pill} <span class="why">held back: ${escapeHtml(cell.value)} - ${why}</span>`
         + `<button type="button" class="link" data-use="${escapeAttr(cell.name)}">use it</button>`
         + mark;
  }
  return `${pill}${mark}<span class="why">${why}</span>`;
}

// A suggestion below the bar is still worth one click when the agent agrees.
$('simForm').addEventListener('click', (event) => {
  const button = event.target.closest('[data-use]');
  if (!button) return;
  const cell = sim.cells.get(button.dataset.use);
  if (!cell || !cell.value) return;
  sim.typed[cell.name] = cell.value;
  const node = document.querySelector(`[data-sim="${CSS.escape(cell.name)}"]`);
  if (node) writeControl(node, cell.value);
  simFill();
});

// -- the scorecard ------------------------------------------------------

function renderSavings(result) {
  const saving = result.savings;
  $('simStatus').hidden = false;
  $('simStatus').className = saving.filled ? 'status ok' : 'status';
  $('simStatus').textContent = result.headline;

  $('simStats').innerHTML = `
    <div class="stat"><b>${saving.filled}</b><span>filled for you</span></div>
    <div class="stat"><b>${saving.left}</b><span>still yours</span></div>
    <div class="stat ${saving.share_saved >= 0.3 ? 'good' : 'warn'}">
      <b>${Math.round(saving.share_saved * 100)}%</b><span>less work</span></div>
    <div class="stat"><b>${saving.saved}</b><span>saved on this form</span></div>`;

  $('simMeterLabel').textContent =
    `${saving.now} to work this form, against ${saving.by_hand} typing it out. `
    + `${saving.keystrokes_saved} fewer keystrokes.`;
  const total = Math.max(1, saving.fields);
  $('simMeter').innerHTML = ['typed', 'filled', 'suggested', 'left'].map((key) => {
    const count = key === 'left' ? saving.left - saving.suggested : saving[key];
    if (count <= 0) return '';
    const kind = key === 'left' ? 'yours' : key;
    return `<span class="seg-${kind}" style="width:${(count / total) * 100}%"
             title="${count} ${kind}"></span>`;
  }).join('');

  explainSaving(result);

  const score = result.score;
  if (!score || !sim.case) {
    $('simScore').textContent = '';
    return;
  }
  const parts = [];
  if (score.checked) {
    parts.push(`${score.right} of ${score.checked} filled values match the real form`);
  }
  if (score.held_back) {
    parts.push(`${score.held_back} more were suggested below the bar, ${score.held_back_right} of them right`);
  }
  if (score.declined) {
    parts.push(`${score.declined} the model declined and the form does have a value for`);
  }
  $('simScore').textContent = parts.join('; ') + '.';
}

// A low saving is the honest answer for a model trained on invented records,
// and leaving it as a bare 3% invites the wrong conclusion. There are two
// reasons it can be low, though, and they call for different answers: a form
// that declares no rules has nothing but the persona's own coherence to
// learn, while a form that declares plenty is already being followed and
// what is left over is the part no rule covers. Telling a form of the second
// kind that synthetic data cost it the saving would be simply untrue.
function explainSaving(result) {
  const node = $('simWhy');
  const saving = result.savings;
  const yours = (state.model && state.model.counts) ? state.model.counts.yours : 0;
  if (saving.share_saved >= 0.25 || !saving.fields) { node.hidden = true; return; }
  node.hidden = false;
  const ruled = ((state.schema && state.schema.fields) || [])
    .filter((field) => field.derived).length;
  const opening =
    `Only ${saving.filled} of ${saving.fields} boxes filled, so the saving is small. `
    + `${yours} of this form's fields are different in every record - names, `
    + `identifiers, free text - and no amount of training data makes those `
    + `predictable. `;
  node.textContent = opening + (ruled
    ? `The rest of the form is already following the ${ruled} rules it declares, `
      + `so what is left is the part no rule covers: the habits only real `
      + `submissions carry, like a mailing address usually copied from the home `
      + `one. Train on real past submissions and this number is what moves.`
    : `The rest is what synthetic records cost you: values invented `
      + `one person at a time carry none of the habits real history has, so the `
      + `model has little to learn from. Declaring the form's own rules is one `
      + `way to give it more to find; training on real past submissions is the `
      + `other, and it is what moves this number furthest.`);
}

// -- the case -----------------------------------------------------------

async function newCase(seed) {
  try {
    const result = await api('/api/simulate/case', {
      model_id: state.model.model_id, seed: seed == null ? '' : seed,
    });
    sim.case = result.case;
    sim.caseSeed = result.seed;
  } catch (error) {
    // Without a case the stage still works; it just cannot mark anything.
    sim.case = null;
    toast(error.message, true);
  }
  sim.typed = {};
  drawForm();
  clearBoxes();
  await simFill();
}

function clearBoxes() {
  document.querySelectorAll('#simForm [data-sim]').forEach((node) => writeControl(node, ''));
}

$('simNew').addEventListener('click', () => withBusy($('simNew'), 'Drawing...', async () => {
  sim.page = 0;
  drawPages();
  await newCase();
  toast('a form the model has not seen');
}));

$('simSeed').addEventListener('click', () => withBusy($('simSeed'), 'Typing...', async () => {
  if (!sim.case) { toast('no form loaded to read from', true); return; }
  sim.seeds.forEach((name) => {
    const value = sim.case[name];
    if (value === '' || value === null || value === undefined) return;
    sim.typed[name] = String(value);
    const node = document.querySelector(`[data-sim="${CSS.escape(name)}"]`);
    if (node) writeControl(node, String(value));
  });
  await simFill();
}));

$('simClear').addEventListener('click', () => {
  sim.typed = {};
  clearBoxes();
  simFill();
});

$('simSweep').addEventListener('click', () => withBusy($('simSweep'), 'Running...', async () => {
  const result = await api('/api/simulate/sweep', {
    model_id: state.model.model_id,
    seeds: sim.seeds,
    count: Number($('simCount').value),
    threshold: simThreshold(),
  });
  sim.sweep = result;
  $('simSweepOut').hidden = false;
  $('simSweepStats').innerHTML = `
    <div class="stat"><b>${result.per_case.filled}</b><span>filled a form</span></div>
    <div class="stat ${result.accuracy >= 0.9 ? 'good' : 'warn'}">
      <b>${Math.round(result.accuracy * 100)}%</b><span>of those right</span></div>
    <div class="stat ${result.share_saved >= 0.3 ? 'good' : 'warn'}">
      <b>${Math.round(result.share_saved * 100)}%</b><span>less work</span></div>
    <div class="stat"><b>${result.per_case.saved}</b><span>saved a form</span></div>`;
  $('simSweepLine').textContent = result.headline;
}));

// --------------------------------------------------------------- library
//
// The list is flat and the lineage is a line under each row, rather than a
// tree view. A tree view of four kinds three deep spends most of its space
// on indentation, and the question a person actually arrives with is "which
// model was that?" - which wants a list sorted by when, with the chain
// spelled out underneath.

const libState = { kind: '', entries: [] };

// An entry another panel has asked to be shown, held until the list it is in
// has been drawn. The Simulate panel names the model it is running and the
// work behind it; this is what makes those names lead somewhere.
let toReveal = null;

function revealInLibrary(entryId) {
  toReveal = entryId;
  // The filter comes off rather than hiding the very thing that was asked
  // for: a schema is not in the list of models.
  libState.kind = '';
  document.querySelectorAll('[data-lib]').forEach(
    (button) => button.classList.toggle('is-on', !button.dataset.lib));
  showPanel('library');
}

const KIND_WORDS = {
  source: 'source', schema: 'schema', dataset: 'data', model: 'model',
  script: 'script',
};

document.querySelectorAll('[data-lib]').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-lib]').forEach((b) => b.classList.remove('is-on'));
    button.classList.add('is-on');
    libState.kind = button.dataset.lib;
    loadLibrary();
  });
});

$('libRefresh').addEventListener('click', () => loadLibrary());

async function loadLibrary() {
  const status = $('libStatus');
  try {
    const result = await api('/api/library',
                             libState.kind ? { kind: libState.kind } : {});
    libState.entries = result.entries;
    $('libWhere').textContent = result.root;
    const totals = result.totals;
    $('libStats').hidden = false;
    $('libStats').innerHTML = Object.entries(totals).map(([kind, count]) =>
      `<div class="stat"><b>${count}</b><span>${escapeHtml(kind)}s</span></div>`).join('')
      + `<div class="stat small"><b>${Math.round(result.bytes / 1024)} KB</b>`
      + `<span>on disk</span></div>`;
    renderLibrary();
    status.hidden = Boolean(result.entries.length);
    if (!result.entries.length) {
      status.className = 'status';
      status.textContent = 'Nothing here yet. Read a form, generate some records '
        + 'and train a model, and each of those will be kept here.';
    }
  } catch (error) {
    toReveal = null;
    status.hidden = false;
    status.className = 'status bad';
    status.textContent = error.message;
  }
}

function renderLibrary() {
  const byId = new Map(libState.entries.map((entry) => [entry.id, entry]));
  $('libList').innerHTML = libState.entries.map((entry) => {
    const detail = Object.entries(entry.meta || {})
      .filter(([, value]) => value !== null && value !== '' && value !== undefined)
      .map(([key, value]) => `${escapeHtml(key)} ${escapeHtml(formatMeta(value))}`)
      .join(' · ');
    // The chain is spelled out with the names it has, falling back to the id
    // for an ancestor that has since been deleted. Each crumb carries its
    // kind, because a schema and the page it was read out of are usually
    // called the same thing and "from claims_intake, claims_intake" tells
    // a reader nothing.
    const chain = (entry.lineage || []).map((id) => {
      const parent = byId.get(id);
      const kind = parent ? KIND_WORDS[parent.kind] : '';
      return `<span class="crumb"><em>${escapeHtml(kind)}</em> `
           + `${escapeHtml(parent ? parent.name : id)}</span>`;
    }).join('<span class="arrow">&rsaquo;</span>');
    const made = (entry.children || []).length;
    return `<article class="lib-row" data-id="${escapeAttr(entry.id)}">
      <div class="lib-kind ${escapeAttr(entry.kind)}">${escapeHtml(KIND_WORDS[entry.kind])}</div>
      <div class="lib-body">
        <h4>${escapeHtml(entry.name)}</h4>
        <p class="muted">${escapeHtml(whenText(entry.created))}
           ${detail ? ' · ' + detail : ''}
           ${made ? ` · ${made} thing(s) made from it` : ''}</p>
        ${chain ? `<p class="lineage">from ${chain}</p>` : ''}
      </div>
      <div class="lib-actions">
        <button class="btn btn-primary small" data-open="${escapeAttr(entry.id)}">Open</button>
        <button class="btn btn-ghost small" data-download="${escapeAttr(entry.id)}">Download</button>
        <button class="btn btn-ghost small" data-drop="${escapeAttr(entry.id)}">Delete</button>
      </div>
    </article>`;
  }).join('');

  if (toReveal) {
    const id = toReveal;
    toReveal = null;
    const row = $('libList').querySelector(`.lib-row[data-id="${CSS.escape(id)}"]`);
    if (!row) {
      toast('that is not in the library any more', true);
    } else {
      row.classList.add('is-found');
      row.scrollIntoView({ block: 'center', behavior: 'smooth' });
      // Long enough to find the row it left you at, short enough not to stay
      // highlighted while you read the rest of the list.
      setTimeout(() => row.classList.remove('is-found'), 3000);
    }
  }
}

// The stored time is ISO 8601 with an offset; showing it raw is the sort of
// thing that reads as unfinished. Rendered in the viewer's own zone, which
// is the one they can compare against a clock.
function whenText(created) {
  const at = new Date(created);
  if (Number.isNaN(at.getTime())) return created || '';
  return at.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function formatMeta(value) {
  if (typeof value === 'number' && value > 0 && value < 1) {
    return `${Math.round(value * 100)}%`;
  }
  return Array.isArray(value) ? value.slice(0, 3).join(', ') : String(value);
}

$('libList').addEventListener('click', async (event) => {
  const open = event.target.closest('[data-open]');
  const save = event.target.closest('[data-download]');
  const drop = event.target.closest('[data-drop]');
  if (open) return openFromLibrary(open.dataset.open);
  if (save) {
    return withBusy(save, '...', async () => {
      const result = await api('/api/library/export', { id: save.dataset.download });
      download(result.filename, result.text, 'application/json;charset=utf-8');
    });
  }
  if (drop) {
    const entry = libState.entries.find((e) => e.id === drop.dataset.drop);
    const made = entry && (entry.children || []).length;
    const question = made
      ? `Delete "${entry.name}" and the ${made} thing(s) made from it?`
      : `Delete "${(entry || {}).name || drop.dataset.drop}"?`;
    if (!window.confirm(question)) return;
    return withBusy(drop, '...', async () => {
      await api('/api/library/delete', { id: drop.dataset.drop, cascade: true });
      toast('deleted');
      await loadLibrary();
    });
  }
});

// Opening an entry restores the whole chain behind it, not just the entry:
// a model arrives with its dataset and its schema in place, so the Train and
// Simulate panels work immediately rather than complaining about what is
// missing.
async function openFromLibrary(id) {
  let result;
  try {
    result = await api('/api/library/open', { id });
  } catch (error) {
    toast(error.message, true);
    return;
  }

  if (result.source !== undefined) {
    state.sourceText = result.source;
    state.kind = result.source_kind || 'html';
    $('source').value = result.source;
    state.ids.source = result.source_id || null;
  }
  if (result.schema) {
    state.schema = result.schema;
    state.summary = null;
    state.ids.schema = result.schema_id || null;
    state.edited = new Set();
    unlock('schema');
    unlock('generate');
    renderSchema();
  }
  if (result.records) {
    state.records = result.records;
    state.columns = Object.keys(result.records[0] || {});
    state.problems = [];
    state.ids.dataset = result.entry.id;
    renderData();
  } else if (result.dataset_id) {
    state.ids.dataset = result.dataset_id;
  }

  const kind = result.entry.kind;
  if (kind === 'script') {
    // A script is read, not resumed. It goes where the live one goes - the
    // log pane on the Train panel - so there is one place a script is ever
    // shown, whether it was written a moment ago or last week.
    $('runStage').textContent = `${result.entry.name}: the script as it ran`;
    $('runLog').textContent = result.script || '';
    showPanel('train');
    toast(`opened ${result.entry.name}`);
    return;
  }
  if (kind === 'model') {
    // The reopened model has no evaluation with it - that was measured
    // against records held back during a run that is over. Saying so is
    // better than showing yesterday's number as if it were this one's.
    state.model = result;
    state.model.model_id = result.model_id;
    state.model.reopened = true;
    state.ids.model = result.entry.id;
    state.typed = {};
    renderTrainResult();
    await refillPreview();
    showPanel('train');
  } else if (kind === 'dataset') {
    showPanel('generate');
  } else {
    showPanel('schema');
  }
  toast(`opened ${result.entry.name}`);
}


// --------------------------------------------------------------- account
//
// Two audiences in one panel. Everybody can change their own password;
// an administrator also gets the list of accounts and the database. The
// server enforces the difference - this only decides what to draw, so
// nobody is shown a button that is going to refuse them.

function renderAccount() {
  const user = state.user;
  $('account').hidden = !user;
  if (!user) return;
  const initials = (user.display_name || user.username).trim()
    .split(/\s+/).slice(0, 2).map((part) => part[0] || '').join('').toUpperCase();
  $('whoami').innerHTML =
    `<span class="initials">${escapeHtml(initials || '?')}</span>`
    + `<span>${escapeHtml(user.display_name || user.username)}</span>`
    + (user.role === 'admin' ? '<span class="role">admin</span>' : '');
  const admin = user.role === 'admin';
  $('adminUsers').hidden = !admin;
  $('adminNew').hidden = !admin;
  $('adminSide').hidden = !admin;
}

$('whoami').addEventListener('click', () => {
  showPanel('account');
  if (state.user && state.user.role === 'admin') {
    loadUsers();
    loadDatabase();
  }
});

$('signOut').addEventListener('click', async () => {
  try {
    await api('/api/auth/logout');
  } catch (error) {
    // Signing out locally is the point; a failed call should not trap
    // somebody in a session they have asked to leave.
  }
  window.location.href = '/login';
});

$('ownSave').addEventListener('click', () => withBusy($('ownSave'), 'Changing...', async () => {
  const status = $('ownStatus');
  status.hidden = true;
  if ($('ownNew').value !== $('ownAgain').value) {
    status.hidden = false;
    status.className = 'status bad';
    status.textContent = 'those two do not match';
    return;
  }
  const result = await api('/api/auth/password',
                           { current: $('ownCurrent').value, new: $('ownNew').value });
  state.csrf = result.csrf;
  state.user = result.user;
  ['ownCurrent', 'ownNew', 'ownAgain'].forEach((id) => { $(id).value = ''; });
  status.hidden = false;
  status.className = 'status ok';
  status.textContent = 'changed, and every other session was signed out';
}));

// ----------------------------------------------------------------- users

async function loadUsers() {
  const status = $('userStatus');
  try {
    const result = await api('/api/admin/users');
    status.hidden = true;
    $('userRows').innerHTML = result.users.map((user) => {
      const you = user.id === result.you;
      const tags = [`<span class="tag ${user.role === 'admin' ? 'admin' : ''}">`
                    + `${escapeHtml(user.role)}</span>`];
      if (!user.active) tags.push('<span class="tag off">disabled</span>');
      if (user.must_change) tags.push('<span class="tag new">new password</span>');
      // The display name goes under the username rather than in a column of
      // its own: six columns and four buttons do not fit side by side, and
      // the two names are one fact about one person anyway.
      return `<tr data-user="${escapeAttr(user.id)}">
        <td>
          <div class="name">${escapeHtml(user.username)}${you ? ' (you)' : ''}</div>
          ${user.display_name && user.display_name !== user.username
            ? `<div class="muted">${escapeHtml(user.display_name)}</div>` : ''}
        </td>
        <td>${tags.join(' ')}</td>
        <td class="num">${user.entries}</td>
        <td>${escapeHtml(user.last_login ? whenText(user.last_login) : 'never')}</td>
        <td><div class="row-acts">
          <button class="btn btn-ghost small" data-act="role">
            ${user.role === 'admin' ? 'Make a user' : 'Make an admin'}</button>
          <button class="btn btn-ghost small" data-act="active">
            ${user.active ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-ghost small" data-act="reset">Reset password</button>
          ${you ? '' : '<button class="btn btn-ghost small" data-act="delete">Delete</button>'}
        </div></td>
      </tr>`;
    }).join('');
  } catch (error) {
    status.hidden = false;
    status.className = 'status bad';
    status.textContent = error.message;
  }
}

$('userRows').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const row = button.closest('[data-user]');
  const id = row.dataset.user;
  const username = row.querySelector('.name').textContent.replace(' (you)', '');
  const admin = row.querySelector('.tag.admin');
  const disabled = row.querySelector('.tag.off');
  const status = $('userStatus');
  status.hidden = true;

  await withBusy(button, 'Working...', async () => {
    if (button.dataset.act === 'role') {
      await api('/api/admin/users/update', { id, role: admin ? 'user' : 'admin' });
    } else if (button.dataset.act === 'active') {
      await api('/api/admin/users/update', { id, active: Boolean(disabled) });
    } else if (button.dataset.act === 'reset') {
      const result = await api('/api/admin/users/password', { id });
      status.hidden = false;
      status.className = 'status ok';
      status.innerHTML = `<div class="handover">New password for `
        + `<b>${escapeHtml(username)}</b>, shown once:<br>`
        + `<code>${escapeHtml(result.password)}</code><br>`
        + `They will be asked to change it when they sign in.</div>`;
    } else if (button.dataset.act === 'delete') {
      const entries = row.querySelector('td.num').textContent.trim();
      const warning = entries === '0' ? '' :
        ` Their ${entries} library entries go with them.`;
      if (!window.confirm(`Delete ${username}?${warning} This cannot be undone.`)) return;
      await api('/api/admin/users/delete', { id });
    }
    await loadUsers();
  });
});

$('newGo').addEventListener('click', () => withBusy($('newGo'), 'Creating...', async () => {
  const status = $('newStatus');
  status.hidden = true;
  const result = await api('/api/admin/users/create', {
    username: $('newUsername').value.trim(),
    display_name: $('newDisplay').value.trim(),
    role: $('newRole').value,
    password: $('newPass').value,
  });
  $('newUsername').value = '';
  $('newDisplay').value = '';
  $('newPass').value = '';
  status.hidden = false;
  status.className = 'status ok';
  status.innerHTML = result.password
    ? `<div class="handover">Created <b>${escapeHtml(result.user.username)}</b>. `
      + `Their password, shown once:<br><code>${escapeHtml(result.password)}</code><br>`
      + `They will be asked to change it when they sign in.</div>`
    : `created ${escapeHtml(result.user.username)}`;
  await loadUsers();
}));

// -------------------------------------------------------------- database

async function loadDatabase() {
  try {
    const result = await api('/api/admin/database');
    const facts = result.database;
    $('dbFacts').innerHTML = [
      ['where', facts.url],
      ['kind', facts.backend],
      ['schema version', String(facts.version)],
      ['tables', facts.tables.join(', ')],
      ['entries', `${result.entries} across every library`],
      ['file library', result.file_library_exists
        ? result.file_library : `${result.file_library} (none there)`],
    ].map(([label, value]) =>
      `<li><b>${escapeHtml(label)}</b> ${escapeHtml(value)}</li>`).join('');
    $('dbImport').disabled = !result.file_library_exists;
  } catch (error) {
    $('dbFacts').innerHTML = `<li>${escapeHtml(error.message)}</li>`;
  }
}

$('dbImport').addEventListener('click', () => withBusy($('dbImport'), 'Importing...', async () => {
  const result = await api('/api/admin/import');
  const status = $('dbStatus');
  status.hidden = false;
  status.className = 'status ok';
  status.textContent = result.copied
    ? `copied ${result.copied} entr(ies) from ${result.from}`
    : `nothing new to copy from ${result.from}`;
}));

// ------------------------------------------------------------------ boot


(async function start() {
  try {
    const meta = await (await fetch('/api/meta')).json();
    if (meta.accounts && !meta.signed_in) {
      window.location.href = '/login';
      return;
    }
    state.user = meta.user || null;
    state.csrf = meta.csrf || '';
    renderAccount();
    state.semanticTypes = meta.semantic_types;
    $('version').textContent = `v${meta.version}`;
    setAlgorithms(meta.algorithms, meta.default_algorithm);
    $('libWhere').textContent = meta.library || '';
    // Installed away from the repository there are no bundled examples, which
    // is normal rather than broken - say so instead of showing an empty list.
    $('examples').innerHTML = meta.examples.length
      ? meta.examples.map((example) => `
          <li><button data-example="${escapeAttr(example.id)}" data-kind="${example.kind}">
            <span class="ex-name">${escapeHtml(example.name)}</span>
            <span class="ex-kind">${escapeHtml(example.label)}</span>
          </button></li>`).join('')
      : '<li class="muted">None bundled with this install. Paste or drop your own form.</li>';

    $('examples').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-example]');
      if (!button) return;
      try {
        const result = await api('/api/example', { id: button.dataset.example });
        loadText(result.content, button.dataset.kind, result.id);
        toast(`loaded ${button.dataset.example}`);
      } catch (error) {
        toast(error.message, true);
      }
    });
  } catch (error) {
    toast('could not reach the FillerAI server', true);
  }
})();
