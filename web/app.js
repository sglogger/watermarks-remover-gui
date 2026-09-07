/* Watermarks Detection & Remover GUI — plain ES2020, no framework, no build step.
 *
 * Two rules worth knowing before editing:
 *  1. Never build markup from server data with innerHTML. Reports, filenames
 *     and scanned text all come from files the user picked; everything here
 *     goes through textContent or createElement.
 *  2. Highlight offsets arrive as UTF-16 indices, matching JavaScript string
 *     indexing, so plain slice() is correct even for astral characters.
 */
'use strict';

const state = {
  formats: null,
  options: {},
  optionDefs: [],
  files: [],
  // The rich (text/html) clipboard flavour of the last paste, when there was
  // one. A textarea only keeps text/plain, which silently drops markup-level
  // markers such as Word's generator tag.
  richPaste: null,
  textScan: null,
  fileScans: [],
  authRequired: false,
  // `tools` from the engine's /capabilities, once /api/status has answered.
  // Some options depend on a binary the engine image may not ship.
  engineTools: null,
  // True when the engine reports at least one usable watermark detector. The
  // published image ships none, and "score the file" then does nothing at all.
  engineDetectors: null,
};

const $ = (id) => document.getElementById(id);

// Shown in the footer next to the engine details. The engine itself is
// credited separately and more prominently in index.html.
const AUTHOR = {
  name: 'Steven Glogger',
  url: 'https://github.com/sglogger/watermarks-remover-gui',
};

// The engine this app drives. Credited prominently in the footer of
// index.html as well; this is the link used in the version rows.
const ENGINE_REPO = 'https://github.com/guillaumemeyer/watermarks-remover';

/* ---------------------------------------------------------------- helpers */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function link(text, href) {
  const anchor = el('a', null, text);
  anchor.href = href;
  anchor.target = '_blank';
  anchor.rel = 'noreferrer noopener';
  return anchor;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : (pluralForm || singular + 's')}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  if (response.status === 401) {
    showLogin();
    throw new Error('Access token required.');
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error(`The server sent an unexpected response (HTTP ${response.status}).`);
    }
  }
  if (!response.ok) {
    throw new Error((data && (data.detail || data.error)) || `Request failed (HTTP ${response.status}).`);
  }
  return data;
}

function busy(button, isBusy, label) {
  button.disabled = isBusy;
  if (isBusy) {
    button.dataset.label = button.textContent;
    clear(button);
    button.appendChild(el('span', 'spinner'));
    button.appendChild(document.createTextNode(' ' + (label || 'Working…')));
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
    delete button.dataset.label;
  }
}

/* ------------------------------------------------------------------ login */

function showLogin() {
  $('login').hidden = false;
  $('login-token').focus();
}

async function submitLogin(event) {
  event.preventDefault();
  const error = $('login-error');
  error.hidden = true;
  try {
    await api('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: $('login-token').value }),
    });
    $('login').hidden = true;
    $('login-token').value = '';
    boot();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
}

/* ----------------------------------------------------------------- status */

function banner(kind, text, href) {
  const node = el('div', `banner banner-${kind}`);
  const paragraph = el('p', null, text);
  if (href) {
    paragraph.appendChild(document.createTextNode(' '));
    paragraph.appendChild(link('Release notes', href));
  }
  node.appendChild(paragraph);
  return node;
}

function renderStatus(status) {
  const pill = $('status-pill');
  clear(pill);
  const engine = status.engine || {};
  const dot = el('span', 'dot ' + (engine.ok ? 'dot-ok' : 'dot-bad'));
  const label = engine.ok
    ? `Engine ${engine.version || 'ready'}`
    : 'Engine unreachable';
  pill.appendChild(dot);
  pill.appendChild(el('span', 'status-text', label));

  const banners = clear($('banners'));
  if (!engine.ok) {
    banners.appendChild(banner('error',
      `Cannot reach the watermarks-remover engine at ${engine.url}. ${engine.error || ''}`.trim()));
  }
  for (const message of (status.contract && status.contract.messages) || []) {
    banners.appendChild(banner('warn', message));
  }
  for (const note of (status.contract && status.contract.notes) || []) {
    banners.appendChild(banner('info', note));
  }
  const release = status.release || {};
  if (release.outdated) {
    banners.appendChild(banner('info',
      `A newer release of the engine is available: ${release.latest} ` +
      `(this stack runs ${release.current}). This is the watermarks-remover engine, ` +
      'not this app — update WR_CORE_TAG in .env, then run ' +
      'docker compose pull && docker compose up -d.',
      release.url));
  }

  const meta = status.metadata_check || {};
  if (meta.enabled && !meta.available) {
    banners.appendChild(banner('warn',
      `The local metadata check is switched on but cannot run: ${meta.error || 'exiftool is unavailable'}. ` +
      'Scans fall back to the engine report alone.'));
  }

  const capabilities = engine.capabilities || null;
  state.engineTools = (capabilities && capabilities.tools) || null;
  state.engineDetectors = detectorsAvailable(capabilities);
  refreshOptionCapabilities();

  renderVersions(status.app || {}, engine, release);
}

// Two independent version numbers are in play and they are easy to confuse:
// this frontend's own, and the separate engine it drives. They get one labelled
// row each, never a single run-on line.
//
// Built from DOM nodes rather than HTML strings — `engine.url` and
// `engine.version` arrive over the wire, and this file never hands server data
// to innerHTML.
function renderVersions(app, engine, release) {
  const list = clear($('versions'));

  function row(label, build) {
    list.appendChild(el('dt', null, label));
    const value = el('dd');
    build(value);
    list.appendChild(value);
  }

  function sep(node) {
    node.appendChild(el('span', 'sep', '·'));
  }

  row('This app', (node) => {
    node.appendChild(link('watermarks-remover-gui', AUTHOR.url));
    node.appendChild(document.createTextNode(' '));
    node.appendChild(el('span', 'ver', app.version ? `v${app.version}` : 'unknown'));
    sep(node);
    node.appendChild(document.createTextNode('by '));
    node.appendChild(link(AUTHOR.name, AUTHOR.url));
  });

  row('Engine', (node) => {
    node.appendChild(link('watermarks-remover', ENGINE_REPO));
    node.appendChild(document.createTextNode(' '));
    node.appendChild(el('span', 'ver', engine.version ? `v${String(engine.version).replace(/^v/, '')}` : 'unreachable'));
    if (release.latest) {
      sep(node);
      node.appendChild(document.createTextNode(
        release.outdated ? `newest release ${release.latest} — update available` : 'up to date'));
    }
    sep(node);
    node.appendChild(document.createTextNode(engine.url));
  });
}

/* ---------------------------------------------------------------- options */

// A checkbox for a boolean option, a select for one the engine types as a
// string enum, a text field for a free-form one. The shape comes from the
// engine's own OpenAPI document by way of /api/formats, so a new option appears
// here without a change in this file -- as long as it is one of these kinds.
function optionControl(def) {
  const id = `opt-${def.name}`;
  if (def.type === 'text') {
    const input = el('input');
    input.type = 'text';
    input.id = id;
    input.spellcheck = false;
    input.autocomplete = 'off';
    if (def.placeholder) input.placeholder = def.placeholder;
    input.value = state.options[def.name] || '';
    // `input`, not `change`: the summary and the stale-results note should
    // follow the field as it is typed, not wait for focus to leave it.
    input.addEventListener('input', () => {
      state.options[def.name] = input.value;
      onOptionsChanged();
    });
    return input;
  }
  if (def.type === 'choice') {
    const select = el('select');
    select.id = id;
    for (const choice of def.choices || []) {
      const opt = el('option', null, choice.label || choice.value);
      opt.value = choice.value;
      if (choice.help) opt.title = choice.help;
      select.appendChild(opt);
    }
    select.value = state.options[def.name];
    select.addEventListener('change', () => {
      state.options[def.name] = select.value;
      onOptionsChanged();
    });
    return select;
  }

  const input = el('input');
  input.type = 'checkbox';
  input.id = id;
  input.checked = Boolean(state.options[def.name]);
  input.addEventListener('change', () => {
    state.options[def.name] = input.checked;
    onOptionsChanged();
  });
  return input;
}

// The help text for the value currently selected. A choice option's risk is
// not uniform across its values, so the per-choice line is what makes "Always"
// re-encoding your images visible at the moment you pick it.
function choiceHelp(def) {
  const current = (def.choices || []).find((c) => c.value === state.options[def.name]);
  return current && current.help ? current.help : '';
}

function renderOptions(defs) {
  state.optionDefs = defs;
  const list = clear($('option-list'));
  for (const def of defs) {
    if (!(def.name in state.options)) state.options[def.name] = def.default;

    const isChoice = def.type === 'choice';
    const isText = def.type === 'text';
    const wrapper = el('div', `option${isChoice ? ' option-choice' : ''}${isText ? ' option-text' : ''}`);
    wrapper.dataset.option = def.name;
    const control = optionControl(def);

    const label = el('label', null, def.label);
    label.htmlFor = control.id;

    // A checkbox reads control-then-label; a select or a text field reads
    // label-then-control, because the control is the wide part of the row.
    if (isChoice || isText) {
      wrapper.appendChild(label);
      wrapper.appendChild(control);
    } else {
      wrapper.appendChild(control);
      wrapper.appendChild(label);
    }
    if (def.help) wrapper.appendChild(el('p', 'help', def.help));
    if (isChoice) {
      const detail = el('p', 'help choice-help', choiceHelp(def));
      wrapper.appendChild(detail);
    }
    // An option that only bites on one kind of file should say so, rather than
    // implying it changes what happens to the PDF sitting beside it.
    if (def.applies_to === 'text') {
      wrapper.appendChild(el('p', 'help', 'Applies to plain text only — Markdown, HTML, PDF, Office and image files ignore it.'));
    }
    if (def.risk) wrapper.appendChild(el('p', 'risk', `Caution: ${def.risk}`));
    list.appendChild(wrapper);
  }
  updateOptionSummary();
  refreshOptionCapabilities();
}

// An option can depend on a tool the engine image does not ship -- the
// published engine has no Ghostscript, so the PDF deep-image pass reports
// itself as skipped rather than failing. Saying so beside the control beats
// letting someone pick "Always" and wonder why nothing changed.
//
// /api/formats answers before /api/status, so this runs twice: once with
// nothing known, and again once capabilities arrive.
function refreshOptionCapabilities() {
  for (const def of state.optionDefs) {
    const wrapper = document.querySelector(`[data-option="${def.name}"]`);
    if (!wrapper) continue;
    const existing = wrapper.querySelector('.capability-note');
    if (existing) existing.remove();
    if (def.requires_detector) {
      if (state.engineDetectors !== false) continue;
      wrapper.appendChild(el('p', 'risk capability-note',
        'This engine build has no watermark detector configured, so scoring ' +
        'reports nothing whichever value you pick.'));
      continue;
    }
    if (!def.requires_tool || !state.engineTools) continue;
    if (state.engineTools[def.requires_tool] !== false) continue;
    wrapper.appendChild(el('p', 'risk capability-note',
      `This engine build has no ${def.requires_tool}, so it reports this pass as ` +
      'skipped whichever value you pick.'));
  }
}

// The engine lists detectors in two places -- `text_detectors` for text and
// `scorers` for images -- and stylometry is always on, so it is excluded: a
// build with nothing but stylometry cannot score a watermark scheme.
function detectorsAvailable(capabilities) {
  if (!capabilities) return null;
  const groups = [capabilities.text_detectors, capabilities.scorers];
  let known = false;
  for (const group of groups) {
    if (!group || typeof group !== 'object') continue;
    for (const [name, value] of Object.entries(group)) {
      if (name === 'stylometry') continue;
      known = true;
      if (value) return true;
    }
  }
  return known ? false : null;
}

function updateOptionSummary() {
  for (const def of state.optionDefs) {
    if (def.type !== 'choice') continue;
    const detail = document.querySelector(`[data-option="${def.name}"] .choice-help`);
    if (detail) detail.textContent = choiceHelp(def);
  }
  const changed = state.optionDefs.filter((d) => state.options[d.name] !== d.default);
  const hint = document.querySelector('.summary-hint');
  hint.textContent = changed.length
    ? `${plural(changed.length, 'option')} changed`
    : 'Safe defaults are applied';
}

function onOptionsChanged() {
  updateOptionSummary();
  for (const id of ['text-result', 'file-result']) {
    const container = $(id);
    if (container.hidden || container.querySelector('.options-stale')) continue;
    const note = banner('info', 'Options changed. Scan again to update the marked positions.');
    note.classList.add('options-stale');
    container.insertBefore(note, container.firstChild);
  }
}

/* -------------------------------------------------------------- highlight */

// Format, control, private-use and non-spacing characters render as nothing,
// so a bare <mark> around them would be invisible. Those get a stand-in glyph.
const INVISIBLE = /^[\p{Cf}\p{Cc}\p{Co}\p{Mn}]+$/u;

function renderHighlight(text, spans) {
  const view = el('pre', 'hl-view');
  view.tabIndex = 0;
  let cursor = 0;
  spans.forEach((span, index) => {
    const start = Math.max(cursor, Math.min(span.start, text.length));
    const end = Math.max(start, Math.min(span.end, text.length));
    if (start > cursor) view.appendChild(document.createTextNode(text.slice(cursor, start)));

    const chunk = text.slice(start, end);
    const mark = el('mark', `hl hl-${span.kind || 'other'}`);
    mark.dataset.index = String(index);
    mark.dataset.kind = span.kind || 'other';
    const codepoints = Array.from(chunk)
      .map((ch) => 'U+' + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0'))
      .slice(0, 4)
      .join(' ');
    const verb = span.action === 'removed' ? 'will be removed'
      : span.action === 'replaced' ? `will become ${JSON.stringify(span.replacement)}`
      : 'will be changed';
    mark.title = span.kind === 'block'
      ? `Marked content (${span.chars} characters) — ${verb}`
      : `${span.label || span.kind} (${codepoints}) — ${verb}`;

    if (INVISIBLE.test(chunk)) {
      mark.appendChild(el('span', 'ghost-glyph', '·'.repeat(Array.from(chunk).length)));
    } else {
      mark.appendChild(document.createTextNode(chunk));
    }
    view.appendChild(mark);
    cursor = end;
  });
  if (cursor < text.length) view.appendChild(document.createTextNode(text.slice(cursor)));
  return view;
}

/* ----------------------------------------------------------- navigation */

// A count alone ("2 regions") does not help anyone find anything, least of all
// when the finding is an invisible character. Every legend entry is therefore a
// button that walks through its own occurrences, and there is a plain
// previous/next pair for walking all of them in document order.

function scrollMarkIntoView(view, mark) {
  // `.hl-view` is position:relative, so offsetTop is relative to it. Scrolling
  // the container by hand keeps the page itself still.
  view.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const target = mark.offsetTop - view.clientHeight / 2 + mark.offsetHeight / 2;
  view.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
}

function createNavigator(view) {
  const marks = Array.from(view.querySelectorAll('mark.hl'));
  let cursor = -1;

  function focusAt(index) {
    if (!marks.length) return;
    cursor = (index + marks.length) % marks.length;
    for (const mark of marks) mark.classList.remove('is-target');
    const mark = marks[cursor];
    mark.classList.add('is-target');
    scrollMarkIntoView(view, mark);
    if (typeof onMove === 'function') onMove(cursor, marks.length);
  }

  let onMove = null;

  return {
    total: marks.length,
    onMove(fn) { onMove = fn; },
    step(delta) { focusAt(cursor + delta); },
    goToKind(kind) {
      // Continue from wherever we are, so repeated clicks cycle that kind.
      for (let offset = 1; offset <= marks.length; offset += 1) {
        const candidate = (cursor + offset + marks.length) % marks.length;
        if (marks[candidate].dataset.kind === kind) {
          focusAt(candidate);
          return;
        }
      }
    },
  };
}

function renderLegend(legend, navigator) {
  const row = el('div', 'legend');
  const template = $('tpl-legend-chip');
  for (const entry of legend) {
    const chip = template.content.firstElementChild.cloneNode(true);
    chip.querySelector('.swatch').classList.add(`swatch-${entry.kind || 'other'}`);
    chip.querySelector('.chip-label').textContent = entry.label || entry.kind;
    chip.querySelector('.chip-count').textContent =
      entry.unit === 'regions' ? `${entry.count} region${entry.count === 1 ? '' : 's'}` : entry.count;

    if (navigator && navigator.total) {
      chip.classList.add('chip-action');
      chip.type = 'button';
      chip.title = `Jump to the next ${entry.label || entry.kind}`;
      chip.addEventListener('click', () => navigator.goToKind(entry.kind || 'other'));
    }
    row.appendChild(chip);
  }
  return row;
}

// Legend, navigator and marked-up text as one unit, so both tabs get identical
// behaviour from one place.
function renderFindings(text, highlight) {
  const fragment = document.createDocumentFragment();
  const view = renderHighlight(text, highlight.spans || []);
  const navigator = createNavigator(view);

  fragment.appendChild(renderLegend(highlight.legend || [], navigator));
  fragment.appendChild(view);

  if (navigator.total > 1) {
    const bar = el('div', 'nav-bar');
    const position = el('span', 'muted nav-position', `${navigator.total} findings`);
    const previous = el('button', 'small ghost', '\u2039 Previous');
    const next = el('button', 'small ghost', 'Next \u203a');
    previous.type = next.type = 'button';
    previous.addEventListener('click', () => navigator.step(-1));
    next.addEventListener('click', () => navigator.step(1));
    navigator.onMove((index, total) => {
      position.textContent = `Finding ${index + 1} of ${total}`;
    });
    const group = el('div', 'button-group');
    group.appendChild(previous);
    group.appendChild(next);
    bar.appendChild(position);
    bar.appendChild(group);
    fragment.appendChild(bar);
  }
  return fragment;
}

/* ---------------------------------------------------- evidence classes */

// v0.7.0 stopped answering "is this watermarked?" with one boolean and started
// reporting each kind of evidence separately, because the four are not
// comparable: an embedded C2PA manifest is a fact about the file, while a
// stylometry score is a guess about its author. Flattening them back into a
// single badge would throw away the distinction the engine went to the trouble
// of drawing, so the classes are listed under the verdict.
const EVIDENCE_LABELS = {
  provenance: 'Provenance metadata',
  layer_a_unicode: 'Invisible characters',
  watermark_detector: 'Watermark detector',
  stylometry: 'Writing-style score',
};

const STRENGTH_LABELS = {
  definitive: 'observed directly',
  deterministic: 'observed directly',
  scheme_specific: 'specific to one scheme',
  heuristic: 'statistical guess',
};

function renderEvidence(evidence) {
  if (!evidence || !evidence.length) return null;
  const box = el('div', 'evidence');
  box.appendChild(el('p', 'evidence-title', evidence.length > 1
    ? 'Evidence found, strongest first'
    : 'Evidence found'));
  const list = el('ul', 'evidence-list');
  for (const entry of evidence) {
    const item = el('li');
    const name = EVIDENCE_LABELS[entry.name]
      || String(entry.name || '').replace(/_/g, ' ');
    item.appendChild(el('span', 'evidence-name', name));
    const strength = STRENGTH_LABELS[entry.strength];
    if (strength) item.appendChild(el('span', 'evidence-strength', strength));
    if (entry.description) item.appendChild(el('p', 'help', entry.description));
    list.appendChild(item);
  }
  box.appendChild(list);
  return box;
}

/* ------------------------------------------------------ report rendering */

// The report belongs to the engine, so it is rendered structurally rather than
// field by field: an upstream change degrades the display instead of breaking it.
const REPORT_LABELS = {
  suspicious_total: 'Suspicious characters',
  length: 'Length',
  hits: 'Findings',
  notes: 'Notes',
  actions: 'Actions taken',
  kind: 'Kind',
  format: 'Format',
  parts: 'Document parts',
  removed: 'Removed',
  metadata: 'Metadata',
};

function humanKey(key) {
  return REPORT_LABELS[key] || key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function describeHit(hit) {
  if (typeof hit !== 'object' || hit === null) return String(hit);
  const bits = [];
  if (hit.label) bits.push(hit.label);
  if (hit.codepoint) bits.push(`(${hit.codepoint})`);
  if (hit.kind) bits.push(`· ${hit.kind}`);
  if (typeof hit.count === 'number') bits.push(`× ${hit.count}`);
  return bits.length ? bits.join(' ') : JSON.stringify(hit);
}

// Keys that describe the engine's own scratch space rather than the file.
const REPORT_NOISE = new Set(['path', 'tmp_path', 'input_path']);

function renderReport(report) {
  const container = el('div');
  if (report === null || report === undefined) return container;

  if (typeof report !== 'object') {
    container.appendChild(el('p', 'muted', String(report)));
    return container;
  }

  const list = el('dl', 'kv');
  let rows = 0;
  const nested = [];

  for (const [key, value] of Object.entries(report)) {
    if (value === null || value === undefined || value === '') continue;
    if (REPORT_NOISE.has(key)) continue;
    if (Array.isArray(value)) {
      if (!value.length) continue;
      const block = el('details', 'report-block');
      block.appendChild(el('summary', null, `${humanKey(key)} (${value.length})`));
      const items = el('ul', 'report-list');
      for (const entry of value.slice(0, 200)) {
        items.appendChild(el('li', null, describeHit(entry)));
      }
      if (value.length > 200) items.appendChild(el('li', 'muted', `… ${value.length - 200} more`));
      block.appendChild(items);
      nested.push(block);
    } else if (typeof value === 'object') {
      const block = el('details', 'report-block');
      block.appendChild(el('summary', null, humanKey(key)));
      block.appendChild(renderReport(value));
      nested.push(block);
    } else {
      list.appendChild(el('dt', null, humanKey(key)));
      list.appendChild(el('dd', null, typeof value === 'boolean' ? (value ? 'yes' : 'no') : value));
      rows += 1;
    }
  }

  if (rows) container.appendChild(list);
  for (const block of nested) container.appendChild(block);

  const raw = el('details', 'report-block');
  raw.appendChild(el('summary', null, 'Raw engine report'));
  raw.appendChild(el('pre', 'report-json', JSON.stringify(report, null, 2)));
  container.appendChild(raw);
  return container;
}

// The engine's own report is rendered above; this is the local exiftool pass,
// and it is labelled as such. Two programs looked at the file and they do not
// always agree — presenting the second one as if it were the engine's answer
// would hide exactly the disagreement that makes it worth running.
function renderMetadataScan(scan, item) {
  const section = el('section', 'metascan');
  if (!scan) return section;

  const flagged = scan.flagged || [];
  const other = scan.other || [];
  const heading = `Metadata check (local ${scan.tool || 'exiftool'}${scan.version ? ' ' + scan.version : ''})`;
  section.appendChild(el('h4', 'metascan-title', heading));

  if (!scan.ok) {
    section.appendChild(el('p', 'muted', scan.error || 'The metadata check did not run.'));
    return section;
  }

  const ai = flagged.filter((entry) => entry.kind === 'ai');
  const privacy = flagged.filter((entry) => (entry.kind || 'privacy') === 'privacy');

  if (!flagged.length) {
    section.appendChild(el('p', 'muted',
      other.length
        ? `No identifying metadata. ${plural(other.length, 'other tag')} read.`
        : 'No metadata found.'));
  } else {
    // The engine names some of these itself, in its own exiftool lines. Saying
    // it missed a tag it reported would be the kind of claim that makes the
    // rest of this section untrustworthy, so it is checked rather than assumed.
    const known = new Set(engineMetaEntries(item || {}).map(tagName));
    const unlisted = flagged.filter((entry) => !known.has(tagName(entry)));
    const counted = `${plural(flagged.length, 'identifying tag')} found`;
    section.appendChild(el('p', 'risk', unlisted.length === flagged.length
      ? `${counted} that the engine report does not list.`
      : unlisted.length
        ? `${counted}, ${unlisted.length} of ${flagged.length > 1 ? 'them' : 'which'} ` +
          'absent from the engine report.'
        : `${counted}. The engine report ${flagged.length === 1 ? 'lists it' : 'lists them'} too.`));

    if (ai.length) {
      section.appendChild(el('p', 'metascan-kind',
        `${plural(ai.length, 'tag')} that the file was machine-generated:`));
      section.appendChild(metaScanList(ai));
    }
    if (privacy.length) {
      section.appendChild(el('p', 'metascan-kind', ai.length
        ? 'And identifying the author, tooling or origin — not evidence of AI:'
        : 'Identifying the author, tooling or origin. Not evidence of AI:'));
      section.appendChild(metaScanList(privacy));
    }
  }

  if (other.length) {
    const block = el('details', 'report-block');
    block.appendChild(el('summary', null, `All other tags (${other.length})`));
    const list = el('dl', 'kv');
    for (const entry of other) {
      list.appendChild(el('dt', null, entry.tag));
      list.appendChild(el('dd', null, entry.value));
    }
    block.appendChild(list);
    section.appendChild(block);
  }
  if (scan.truncated) {
    section.appendChild(el('p', 'muted', 'The tag list was truncated.'));
  }
  if (scan.error) section.appendChild(el('p', 'muted', scan.error));
  return section;
}

function metaScanList(entries) {
  const list = el('dl', 'kv');
  for (const entry of entries) {
    list.appendChild(el('dt', null, `${entry.tag} · ${entry.reason}`));
    list.appendChild(el('dd', null, entry.value));
  }
  return list;
}

/* ------------------------------------------------------------ rich paste */

// Pasting out of Word, a browser or an editor puts several flavours on the
// clipboard. The textarea keeps only text/plain, and that flavour genuinely
// loses findings: a Word paste carries `<meta name=Generator …>` in its HTML
// flavour and nothing of the sort in its plain one. So the HTML flavour is
// kept aside, and offered as an extra way to read the same paste.
//
// RTF is deliberately not offered: the engine has no RTF pipeline, so there
// would be nothing to send it to.

const RICH_OPTION = 'rich';

function captureRichPaste(event) {
  const data = event.clipboardData;
  const html = data ? data.getData('text/html') : '';
  if (!html || !html.trim()) {
    state.richPaste = null;
    refreshRichOption();
    return;
  }
  // The textarea value only updates after this handler returns.
  setTimeout(() => {
    state.richPaste = { html, plain: $('text-input').value };
    refreshRichOption();
  }, 0);
}

// The captured markup describes one exact paste. As soon as the box holds
// something else, it no longer describes what is on screen.
function invalidateRichPaste() {
  if (state.richPaste && $('text-input').value !== state.richPaste.plain) {
    state.richPaste = null;
    if ($('text-format').value === RICH_OPTION) $('text-format').value = 'text';
    refreshRichOption();
  }
}

function refreshRichOption() {
  const option = $('text-format').querySelector(`option[value="${RICH_OPTION}"]`);
  const note = $('rich-note');
  const available = Boolean(state.richPaste);

  // The option is always listed, so the capability is visible before anyone
  // has done the one thing that unlocks it. It is only selectable once there
  // is captured markup for it to scan.
  if (option) option.disabled = !available;
  if (!available && $('text-format').value === RICH_OPTION) {
    $('text-format').value = 'text';
  }

  clear(note);
  note.className = available ? 'muted rich-note is-ready' : 'muted rich-note';

  if (!available) {
    note.appendChild(el('strong', null, 'Rich text (as pasted)'));
    note.appendChild(document.createTextNode(
      ' unlocks when you paste from a formatted source such as Word or a web ' +
      'page. Such a paste also carries markup, and the markup is where ' +
      'generator tags and similar markers live — the plain text drops them. ' +
      '(RTF is not offered: the engine has no pipeline for it.)'));
  } else {
    note.appendChild(el('strong', null, 'That paste carried formatting.'));
    note.appendChild(document.createTextNode(
      ' Choosing Rich text (as pasted) scans the markup behind it rather than ' +
      'the plain text in the box, which finds markup-level markers the plain ' +
      'text drops. Its cleaned output is HTML, so for text you will paste back ' +
      'into a document, stay on Plain text.'));
  }
  note.hidden = false;
}

/* -------------------------------------------------------------- text tab */

async function scanText() {
  const button = $('text-scan');
  const container = $('text-result');
  const chosen = $('text-format').value;
  const rich = chosen === RICH_OPTION && state.richPaste;
  // A rich scan sends the clipboard's markup through the HTML pipeline; the
  // plain path sends whatever is in the box.
  const text = rich ? state.richPaste.html : $('text-input').value;
  const format = rich ? 'html' : chosen;
  if (!text.trim()) return;

  busy(button, true, 'Scanning…');
  try {
    const data = await api('/api/scan/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, format, options: state.options }),
    });
    state.textScan = { item: (data.items || [])[0] || null, text, rich: Boolean(rich) };
    renderTextResult(data.warnings || []);
  } catch (err) {
    clear(container).appendChild(banner('error', err.message));
    container.hidden = false;
  } finally {
    busy(button, false);
  }
}

function renderTextResult(warnings) {
  const container = clear($('text-result'));
  container.hidden = false;
  const scan = state.textScan;
  const item = scan && scan.item;

  for (const warning of warnings) container.appendChild(banner('warn', warning));
  if (!item) return;

  const card = el('div', 'card');
  container.appendChild(card);

  if (scan.rich) {
    card.appendChild(banner('info',
      'Scanned the formatting this paste carried, not the plain text in the box. ' +
      'Removing here produces cleaned HTML markup.'));
  }

  if (!item.ok) {
    card.appendChild(summaryLine('error', item.error || 'The engine could not process this text.'));
    return;
  }

  const highlight = item.highlight || { spans: [], legend: [] };

  if (!item.suspicious || !highlight.spans.length) {
    card.appendChild(summaryLine('clean', 'No watermarks found in this text.'));
    if (item.report) card.appendChild(renderReport(item.report));
    return;
  }

  card.appendChild(summaryLine('found', describeFindings(highlight) + ' found.'));
  const evidence = renderEvidence(item.evidence);
  if (evidence) card.appendChild(evidence);
  card.appendChild(renderFindings(scan.text, highlight));

  const actions = el('div', 'row row-between actions');
  actions.appendChild(el('span', 'muted', 'Removing rewrites the text; nothing is stored on the server.'));
  const removeButton = el('button', 'primary', 'Remove watermarks');
  removeButton.addEventListener('click', () => cleanText(removeButton, card));
  actions.appendChild(removeButton);
  card.appendChild(actions);

  if (item.report) card.appendChild(renderReport(item.report));
}

async function cleanText(button, card) {
  const item = state.textScan && state.textScan.item;
  if (!item || !item.id) return;
  busy(button, true, 'Removing…');
  try {
    const data = await api('/api/clean', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [item.id], options: state.options }),
    });
    const result = (data.items || [])[0];
    if (!result || !result.ok) throw new Error((result && result.error) || 'Removal failed.');

    const output = el('div', 'card');
    output.appendChild(summaryLine(
      result.verified === false ? 'found' : 'clean', verdictText(result)));
    for (const leftover of result.remaining_findings || []) {
      output.appendChild(el('p', 'muted', `Still flagged: ${leftover}`));
    }
    output.appendChild(el('pre', 'plain-view', result.text !== null ? result.text : ''));

    const row = el('div', 'row actions');
    const copyButton = el('button', 'primary', 'Copy cleaned text');
    copyButton.addEventListener('click', () => copyText(result.text || '', copyButton));
    row.appendChild(copyButton);
    if (!state.textScan.rich) {
      // Putting cleaned HTML markup back in the box would be nonsense.
      const useButton = el('button', 'ghost', 'Replace input');
      useButton.addEventListener('click', () => {
        $('text-input').value = result.text || '';
        state.richPaste = null;
        refreshRichOption();
        updateTextCount();
        $('text-result').hidden = true;
      });
      row.appendChild(useButton);
    }
    output.appendChild(row);

    card.parentNode.appendChild(output);
    button.disabled = true;
    button.textContent = 'Removed';
  } catch (err) {
    card.appendChild(banner('error', err.message));
  } finally {
    if (!button.disabled) busy(button, false);
  }
}

// `verified` is the reliable signal; a count is only available for reports that
// carry one, so never render a bare "null markers left".
function verdictText(result) {
  if (result.verified !== false) return 'Cleaned — re-scanned and free of detectable marks.';
  if (typeof result.remaining_hits === 'number' && result.remaining_hits > 0) {
    return `Cleaned, but ${plural(result.remaining_hits, 'marker')} still detected.`;
  }
  if ((result.remaining_metadata || []).length) {
    return `Cleaned, but ${plural(result.remaining_metadata.length, 'identifying tag')} ` +
      'survived in the file metadata.';
  }
  return 'Cleaned, but the engine still flags this file. See below.';
}

async function copyText(text, button) {
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'Copied';
  } catch (err) {
    // Clipboard access needs a secure context; fall back to a manual selection.
    const scratch = el('textarea');
    scratch.value = text;
    document.body.appendChild(scratch);
    scratch.select();
    const ok = document.execCommand && document.execCommand('copy');
    document.body.removeChild(scratch);
    button.textContent = ok ? 'Copied' : 'Press Ctrl/Cmd+C';
  }
  setTimeout(() => { button.textContent = original; }, 1800);
}

function summaryLine(kind, text) {
  const line = el('div', 'summary-line');
  const verdict = el('span', `verdict verdict-${kind === 'clean' ? 'clean' : kind === 'error' ? 'error' : 'found'}`);
  verdict.appendChild(el('span', null, kind === 'clean' ? '✓' : kind === 'error' ? '✕' : '⚠'));
  verdict.appendChild(el('span', null, text));
  line.appendChild(verdict);
  return line;
}

function updateTextCount() {
  const length = $('text-input').value.length;
  $('text-count').textContent = plural(length, 'character');
}

/* ------------------------------------------------------------- files tab */

function setFiles(fileList) {
  const max = (state.formats && state.formats.max_files) || 25;
  state.files = Array.from(fileList).slice(0, max);
  const actions = $('file-actions');
  actions.hidden = state.files.length === 0;
  if (state.files.length) {
    const bytes = state.files.reduce((sum, f) => sum + f.size, 0);
    $('file-count').textContent = `${plural(state.files.length, 'file')} · ${formatBytes(bytes)}`;
  }
  if (fileList.length > max) {
    clear($('file-result')).appendChild(
      banner('warn', `Only the first ${max} files are kept; ${fileList.length} were selected.`));
    $('file-result').hidden = false;
  }
}

async function scanFiles() {
  if (!state.files.length) return;
  const button = $('file-scan');
  const container = $('file-result');
  const body = new FormData();
  for (const file of state.files) body.append('files', file, file.name);
  body.append('options', JSON.stringify(state.options));

  busy(button, true, 'Scanning…');
  try {
    const data = await api('/api/scan/files', { method: 'POST', body });
    state.fileScans = data.items || [];
    renderFileResults(data.warnings || []);
  } catch (err) {
    clear(container).appendChild(banner('error', err.message));
    container.hidden = false;
  } finally {
    busy(button, false);
  }
}

function renderFileResults(warnings) {
  const container = clear($('file-result'));
  container.hidden = false;
  for (const warning of warnings) container.appendChild(banner('warn', warning));

  const card = el('div', 'card');
  container.appendChild(card);

  const items = state.fileScans;
  const failed = items.filter((i) => !i.ok);
  // Two different things, kept apart on purpose. `suspicious` is the watermark
  // verdict; privacy tags are an author name or a GPS fix, which are worth
  // removing but say nothing about how the file was made.
  const watermarked = items.filter((i) => i.ok && i.suspicious && i.id);
  const metaOnly = items.filter((i) => i.ok && i.id && !i.suspicious && privacyTags(i).length);
  const marked = watermarked.concat(metaOnly);

  let headline;
  if (watermarked.length && metaOnly.length) {
    headline = `${plural(watermarked.length, 'file')} ${verb(watermarked.length, 'contains', 'contain')} ` +
      `watermarks, ${plural(metaOnly.length, 'other')} ${verb(metaOnly.length, 'carries', 'carry')} ` +
      'identifying metadata.';
  } else if (watermarked.length) {
    headline = `${plural(watermarked.length, 'file')} ${verb(watermarked.length, 'contains', 'contain')} watermarks.`;
  } else if (metaOnly.length) {
    headline = `No watermarks found, but ${plural(metaOnly.length, 'file')} ` +
      `${verb(metaOnly.length, 'carries', 'carry')} identifying metadata.`;
  } else {
    headline = items.some((i) => i.ok)
      ? 'No watermarks found in the scanned files.'
      : 'No files could be scanned.';
  }
  card.appendChild(summaryLine(marked.length ? 'found' : failed.length === items.length ? 'error' : 'clean', headline));
  if (metaOnly.length) {
    const keeps = state.options.strip_all_metadata !== true
      && state.options.keep_non_ai_metadata !== false;
    card.appendChild(el('p', 'muted',
      'Identifying metadata is what a file says about who and what made it — an author ' +
      'name, a company, a camera, a location. It is not evidence of a watermark, and no ' +
      `watermark was found in ${metaOnly.length > 1 ? 'those files' : 'that file'}.` +
      (keeps
        ? ' The current options keep it: turn on "Strip all metadata" to remove it.'
        : ' The current options remove it.')));
  }

  const digest = renderMetadataDigest(items);
  if (digest) card.appendChild(digest);

  const list = el('div', 'file-list');
  items.forEach((item, index) => list.appendChild(renderFileRow(item, index)));
  card.appendChild(list);

  if (marked.length) {
    const actions = el('div', 'row row-between actions');
    actions.appendChild(el('span', 'muted', 'Cleaned copies are offered as downloads; originals are untouched.'));
    const label = marked.length > 1
      ? `Remove from ${plural(marked.length, 'file')}`
      : watermarked.length ? 'Remove watermarks' : 'Remove identifying metadata';
    const button = el('button', 'primary', label);
    button.addEventListener('click', () => cleanFiles(marked.map((i) => i.id), button, card));
    actions.appendChild(button);
    card.appendChild(actions);
  }
}

// Tags that describe the pipe exiftool was fed, not the file that came down it.
// Mirrors `_PIPE_TAGS` in app/metascan.py, which drops the same ones from the
// local pass; the engine leaves them in its own lines.
const META_PIPE_TAGS = new Set([
  'ExifTool:ExifToolVersion',
  'File:FileName',
  'File:Directory',
  'File:FileSize',
  'File:FileModifyDate',
  'File:FileAccessDate',
  'File:FileInodeChangeDate',
  'File:FilePermissions',
]);

// The engine reports its own exiftool pass as pre-formatted console lines —
// "[XMP-dc]        Creator                         : Francesco Caiafa" — under
// report.tools.exiftool.interesting_lines. Split back into tag and value so
// they read like every other metadata row instead of a wall of padding.
function engineMetaEntries(item) {
  const tools = (item.report && item.report.tools) || null;
  const exif = tools && typeof tools === 'object' ? tools.exiftool : null;
  const lines = (exif && exif.interesting_lines) || [];
  const entries = [];
  for (const raw of lines) {
    const line = String(raw);
    const match = /^\[([^\]]*)\]\s*(.+?)\s*:\s?(.*)$/.exec(line);
    if (!match) {
      entries.push({ tag: line.trim(), value: '' });
      continue;
    }
    const tag = `${match[1].trim()}:${match[2].trim()}`;
    if (META_PIPE_TAGS.has(tag)) continue;
    entries.push({ tag, value: match[3].trim() });
  }
  return entries;
}

// Every metadata tag anyone read, from every file in the batch, in one place.
// Two passes end up here: the engine's own exiftool lines, carried in its
// report, and — when GUI_EXIFTOOL is on — the fuller local pass, which also
// says which tags identify someone. The per-file rows below hold the same data,
// but only for the row you happen to open; a watermark-free verdict still
// leaves the question "what is actually written in these files?", and this
// answers it in one click.
function renderMetadataDigest(items) {
  const groups = [];
  let flaggedTotal = 0;
  let tagTotal = 0;

  for (const item of items) {
    if (!item.ok) continue;
    const scan = item.metadata_scan || null;
    const engine = engineMetaEntries(item);
    if (!scan && !engine.length) continue;
    const flagged = (scan && scan.ok && scan.flagged) || [];
    const other = (scan && scan.ok && scan.other) || [];
    if (!flagged.length && !other.length && !engine.length && !(scan && !scan.ok)) continue;
    flaggedTotal += flagged.length;
    tagTotal += flagged.length + other.length + engine.length;
    groups.push({ item, scan, engine, flagged, other });
  }
  if (!groups.length) return null;

  const block = el('details', 'report-block meta-digest');
  const summary = el('summary');
  summary.appendChild(el('span', null,
    `All metadata found (${plural(tagTotal, 'tag')} in ${plural(groups.length, 'file')})`));
  if (flaggedTotal) {
    summary.appendChild(el('span', 'badge badge-found', `${flaggedTotal} identifying`));
  }
  block.appendChild(summary);

  const body = el('div', 'meta-digest-body');
  for (const group of groups) {
    const { item, scan, engine, flagged, other } = group;
    const section = el('section', 'meta-digest-file');
    section.appendChild(el('h5', 'meta-digest-name', item.name));

    if (engine.length) {
      section.appendChild(el('p', 'meta-digest-source', 'Engine report · exiftool'));
      section.appendChild(metaList(engine));
    }

    if (scan && !scan.ok) {
      section.appendChild(el('p', 'meta-digest-source',
        `Local ${scan.tool || 'exiftool'} check`));
      section.appendChild(el('p', 'muted', scan.error || 'The metadata check did not run.'));
    } else if (scan) {
      const tool = `Local ${scan.tool || 'exiftool'}${scan.version ? ' ' + scan.version : ''}`;
      section.appendChild(el('p', 'meta-digest-source', tool));
      if (flagged.length) section.appendChild(metaList(flagged, true));
      if (other.length) section.appendChild(metaList(other));
      if (!flagged.length && !other.length) {
        section.appendChild(el('p', 'muted', 'No metadata found in this file.'));
      }
      if (scan.truncated) section.appendChild(el('p', 'muted', 'The tag list was truncated.'));
      if (scan.error) section.appendChild(el('p', 'muted', scan.error));
    }
    body.appendChild(section);
  }

  const unchecked = items.filter((item) =>
    item.ok && !item.metadata_scan && !engineMetaEntries(item).length).length;
  if (unchecked) {
    body.appendChild(el('p', 'muted',
      `${plural(unchecked, 'file')} carried no metadata either pass could read.`));
  }
  block.appendChild(body);
  return block;
}

// A tag is worth showing even when its value is empty — "Title :" with nothing
// after it means the field exists in the file, which is not the same as absent.
function metaList(entries, flagged) {
  const list = el('dl', 'kv');
  for (const entry of entries) {
    const term = el('dt', flagged ? 'meta-flagged' : null);
    term.appendChild(el('span', null, entry.tag));
    if (flagged && entry.reason) term.appendChild(el('span', 'meta-reason', entry.reason));
    list.appendChild(term);
    list.appendChild(entry.value
      ? el('dd', null, entry.value)
      : el('dd', 'muted', '(empty)'));
  }
  return list;
}

function renderFileRow(item, index) {
  const row = el('div', 'file-row');
  row.dataset.id = item.id || '';

  const head = el('button', 'file-head');
  head.type = 'button';
  head.appendChild(el('span', 'file-name', item.name));
  if (item.kind && item.ok) head.appendChild(el('span', 'badge badge-kind', item.kind));

  const status = !item.ok ? ['badge-error', 'error']
    : item.suspicious ? ['badge-found', hitLabel(item)]
    : privacyTags(item).length ? ['badge-meta', 'metadata']
    : ['badge-clean', 'clean'];
  const badge = el('span', `badge ${status[0]}`, status[1]);
  badge.dataset.role = 'status';
  head.appendChild(badge);

  const body = el('div', 'file-body');
  body.hidden = true;
  body.id = `file-body-${index}`;
  head.setAttribute('aria-expanded', 'false');
  head.setAttribute('aria-controls', body.id);
  head.addEventListener('click', () => {
    body.hidden = !body.hidden;
    head.setAttribute('aria-expanded', String(!body.hidden));
  });

  if (!item.ok) {
    body.appendChild(el('p', 'error', item.error || 'This file could not be processed.'));
  } else {
    const meta = el('dl', 'kv');
    meta.appendChild(el('dt', null, 'Size'));
    meta.appendChild(el('dd', null, formatBytes(item.size)));
    meta.appendChild(el('dt', null, 'Detected as'));
    meta.appendChild(el('dd', null, item.kind));
    body.appendChild(meta);

    const evidence = renderEvidence(item.evidence);
    if (evidence) body.appendChild(evidence);

    if (item.text !== null && item.text !== undefined && item.highlight) {
      body.appendChild(renderFindings(item.text, item.highlight));
    } else if (item.highlight && (item.highlight.legend || []).length) {
      body.appendChild(renderLegend(item.highlight.legend, null));
    } else if (item.suspicious) {
      const raisedBy = item.flagged_by || [];
      body.appendChild(el('p', 'muted', aiTags(item).length && !raisedBy.includes('engine')
        ? 'Flagged by the local metadata check below, not by the engine report.'
        : 'This format cannot be marked up in place; the engine report below lists what was found.'));
    } else if (privacyTags(item).length) {
      body.appendChild(el('p', 'muted',
        'No watermark found. What is listed below is metadata the file carries about ' +
        'its author, tooling or origin.'));
    }
    if (item.report) body.appendChild(renderReport(item.report));
    if (item.metadata_scan) body.appendChild(renderMetadataScan(item.metadata_scan, item));
  }

  row.appendChild(head);
  row.appendChild(body);
  return row;
}

function verb(count, singular, pluralForm) {
  return count === 1 ? singular : pluralForm;
}

// Tags the server classified as a privacy leak rather than as an AI marker:
// an author, a company, a device, a location. `kind` is absent in responses
// from an older server, where every flagged tag was treated as a finding.
function privacyTags(item) {
  return ((item.metadata_scan || {}).flagged || [])
    .filter((entry) => (entry.kind || 'privacy') === 'privacy');
}

function aiTags(item) {
  return ((item.metadata_scan || {}).flagged || []).filter((entry) => entry.kind === 'ai');
}

// The last segment of a tag, lowercased: the engine reports "XMP-dc:Creator"
// (group family 1) and the local pass "XMP:Creator" (family 0), so only the
// name after the colon can be compared between the two.
function tagName(entry) {
  return String(entry.tag || '').split(':').pop().toLowerCase();
}

// "8 hidden characters and 1 marked block" reads far better than "148 characters",
// which is what you get if you count a deleted <metadata> element by the letter.
function describeFindings(highlight) {
  const chars = highlight.carrier_chars || 0;
  const blocks = highlight.block_regions || 0;
  const parts = [];
  if (chars) parts.push(plural(chars, 'hidden character'));
  if (blocks) parts.push(plural(blocks, 'marked block'));
  if (!parts.length) parts.push(plural(highlight.changed_chars || 0, 'change'));
  return parts.join(' and ');
}

function hitLabel(item) {
  if (!item.suspicious) return 'metadata';
  if (!item.highlight) return 'found';
  const chars = item.highlight.carrier_chars || 0;
  const blocks = item.highlight.block_regions || 0;
  if (chars && blocks) return `${chars} + ${blocks}`;
  if (chars) return `${chars} hidden`;
  if (blocks) return plural(blocks, 'block');
  return 'found';
}

async function cleanFiles(ids, button, card) {
  busy(button, true, 'Removing…');
  try {
    const data = await api('/api/clean', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, options: state.options }),
    });
    const results = data.items || [];
    for (const warning of data.warnings || []) card.appendChild(banner('warn', warning));

    const cleaned = [];
    for (const result of results) {
      const row = card.querySelector(`.file-row[data-id="${cssEscape(result.id || '')}"]`);
      if (!row) continue;
      const badge = row.querySelector('[data-role="status"]');
      if (!result.ok) {
        badge.className = 'badge badge-error';
        badge.textContent = 'failed';
        row.querySelector('.file-body').appendChild(el('p', 'error', result.error || 'Removal failed.'));
        continue;
      }
      cleaned.push(result);
      badge.className = 'badge ' + (result.verified === false ? 'badge-found' : 'badge-clean');
      badge.textContent = result.verified === false ? 'still flagged' : 'cleaned';

      const body = row.querySelector('.file-body');
      if (result.verified === false) {
        body.appendChild(el('p', 'risk', verdictText(result)));
        for (const leftover of result.remaining_findings || []) {
          body.appendChild(el('p', 'muted', `Still flagged: ${leftover}`));
        }
        for (const tag of result.remaining_metadata || []) {
          body.appendChild(el('p', 'muted', `Metadata still present: ${tag}`));
        }
      }
      const link = el('a', null, `Download ${result.cleaned_name}`);
      link.href = result.download_url;
      link.setAttribute('download', result.cleaned_name);
      const wrapper = el('p', 'actions');
      wrapper.appendChild(link);
      body.appendChild(wrapper);
      body.hidden = false;
      row.querySelector('.file-head').setAttribute('aria-expanded', 'true');
    }

    button.disabled = true;
    button.textContent = `Cleaned ${plural(cleaned.length, 'file')}`;

    if (cleaned.length > 1) {
      const zip = el('a', null, 'Download all as ZIP');
      zip.href = '/api/download.zip?ids=' + cleaned.map((r) => encodeURIComponent(r.id)).join(',');
      zip.setAttribute('download', 'cleaned-files.zip');
      const wrapper = el('p', 'actions');
      wrapper.appendChild(zip);
      card.appendChild(wrapper);
    }
  } catch (err) {
    card.appendChild(banner('error', err.message));
    busy(button, false);
  }
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/["\\]/g, '\\$&');
}

/* ------------------------------------------------------------------ wiring */

function switchTab(target) {
  const isText = target === 'text';
  $('tab-text').classList.toggle('is-active', isText);
  $('tab-files').classList.toggle('is-active', !isText);
  $('tab-text').setAttribute('aria-selected', String(isText));
  $('tab-files').setAttribute('aria-selected', String(!isText));
  $('panel-text').hidden = !isText;
  $('panel-files').hidden = isText;
}

function wireDropzone() {
  const zone = $('dropzone');
  const input = $('file-input');
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener('change', () => setFiles(input.files));

  for (const name of ['dragenter', 'dragover']) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add('is-over');
    });
  }
  for (const name of ['dragleave', 'drop']) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove('is-over');
    });
  }
  zone.addEventListener('drop', (event) => {
    if (event.dataTransfer && event.dataTransfer.files.length) setFiles(event.dataTransfer.files);
  });
}

async function boot() {
  try {
    const formats = await api('/api/formats');
    state.formats = formats;
    $('file-input').accept = formats.accept;
    $('dropzone-hint').textContent =
      `${formats.extensions.map((e) => e.replace('.', '').toUpperCase()).join(', ')} · ` +
      `up to ${formats.max_upload_mb} MB each, ${formats.max_files} files at a time. No audio or video.`;
    renderOptions(formats.options || []);
  } catch (err) {
    if (err.message !== 'Access token required.') {
      clear($('banners')).appendChild(banner('error', err.message));
    }
    return;
  }

  try {
    renderStatus(await api('/api/status'));
  } catch (err) {
    clear($('banners')).appendChild(banner('error', err.message));
  }
}

function init() {
  $('login-form').addEventListener('submit', submitLogin);
  $('tab-text').addEventListener('click', () => switchTab('text'));
  $('tab-files').addEventListener('click', () => switchTab('files'));
  $('text-scan').addEventListener('click', scanText);
  $('text-input').addEventListener('input', () => {
    invalidateRichPaste();
    updateTextCount();
  });
  $('text-input').addEventListener('paste', captureRichPaste);
  $('text-clear').addEventListener('click', () => {
    $('text-input').value = '';
    state.richPaste = null;
    refreshRichOption();
    updateTextCount();
    $('text-result').hidden = true;
  });
  $('file-scan').addEventListener('click', scanFiles);
  $('file-clear').addEventListener('click', () => {
    state.files = [];
    state.fileScans = [];
    $('file-input').value = '';
    $('file-actions').hidden = true;
    $('file-result').hidden = true;
  });
  wireDropzone();
  refreshRichOption();
  updateTextCount();
  boot();
}

document.addEventListener('DOMContentLoaded', init);
