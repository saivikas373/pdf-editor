// Modal dialogs for document tools and downloads.
import { S, emit } from './state.js';
import { $, h, clamp, toast, downloadBlob, fmtBytes, isMac, setLoading } from './util.js';
import { icon } from './icons.js';
import { mutate, postForFile, docUrl } from './api.js';
import { cssFont } from './objects.js';

const root = $('#dialogs');
const K = isMac ? '⌘' : 'Ctrl+';

export function openDialog({ title, subtitle, body = [], actions = [], wide = false, onClose, footNote }) {
  const backdrop = h('div', { class: 'dialog-backdrop' });
  const dialog = h('div', { class: `dialog${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true' });
  const close = () => {
    backdrop.remove();
    document.removeEventListener('keydown', onKey, true);
    onClose?.();
  };
  const headEl = h('div', { class: 'dialog-head' },
    h('div', { style: { flex: '1' } }, h('h2', { text: title }), subtitle ? h('div', { class: 'sub', text: subtitle }) : null),
    h('button', { class: 'icon-btn', html: icon('x'), 'aria-label': 'Close', onclick: close }));
  const bodyEl = h('div', { class: 'dialog-body' }, ...body);
  const foot = h('div', { class: 'dialog-foot' }, h('span', { class: 'left', text: footNote || '' }));
  const buttons = [];
  let primary = null;
  const ctx = { close, dialog, setNote: (t) => { foot.querySelector('.left').textContent = t; } };
  for (const a of actions) {
    const b = h('button', { class: `btn ${a.kind || 'secondary'}`, html: a.icon ? `${icon(a.icon)}<span>${a.label}</span>` : a.label });
    b.addEventListener('click', async () => {
      if (!a.onClick) return close();
      buttons.forEach((x) => { x.disabled = true; });
      try {
        const keep = await a.onClick(ctx);
        if (keep !== false) close();
      } catch (err) {
        toast(err.message || String(err), { type: 'error' });
      } finally {
        buttons.forEach((x) => { x.disabled = false; });
      }
    });
    if (a.kind === 'primary') primary = b;
    buttons.push(b);
    foot.append(b);
  }
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === 'Enter' && primary && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') {
      e.preventDefault();
      primary.click();
    }
    e.stopPropagation();
  };
  document.addEventListener('keydown', onKey, true);
  backdrop.addEventListener('pointerdown', (e) => { if (e.target === backdrop) close(); });
  dialog.append(headEl, bodyEl);
  if (actions.length) dialog.append(foot);
  backdrop.append(dialog);
  root.append(backdrop);
  setTimeout(() => dialog.querySelector('input:not([type=checkbox]):not([type=radio]), select, textarea')?.focus(), 30);
  return ctx;
}

// ------------------------------------------------------------------ small form helpers
const field = (label, control, hintText) => h('div', { class: 'field' }, h('label', { text: label }), control, hintText ? h('div', { class: 'field-label', style: { fontWeight: 400, color: 'var(--text-3)' }, text: hintText }) : null);
const input = (attrs = {}) => h('input', { class: 'input', ...attrs });
const check = (label, checked = false) => {
  const cb = h('input', { type: 'checkbox', checked });
  return { el: h('label', { class: 'check-row' }, cb, h('span', { text: label })), cb };
};
function seg(options, value) {
  let current = value;
  const el = h('div', { class: 'seg' });
  for (const o of options) {
    const b = h('button', { class: o.value === value ? 'on' : '', type: 'button', html: o.icon ? `${icon(o.icon)}<span>${o.label}</span>` : o.label });
    b.addEventListener('click', () => {
      el.querySelectorAll('button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      current = o.value;
      el.dispatchEvent(new Event('change'));
    });
    el.append(b);
  }
  return { el, get value() { return current; } };
}

function currentPageLabel() {
  return String(S.currentPage + 1);
}

export function parsePageSpec(spec, count) {
  const text = String(spec || '').trim().toLowerCase();
  if (!text || text === 'all') return [...Array(count).keys()];
  const out = [];
  for (const part of text.split(/[,;\s]+/)) {
    if (!part) continue;
    const m = /^(\d*)\s*-\s*(\d*)$/.exec(part);
    let a; let b;
    if (m) {
      a = m[1] ? +m[1] : 1;
      b = m[2] ? +m[2] : count;
    } else if (/^\d+$/.test(part)) {
      a = b = +part;
    } else {
      throw new Error(`Invalid page range "${part}"`);
    }
    if (a < 1 || b > count || a > b) throw new Error(`Page range "${part}" is outside 1–${count}`);
    for (let i = a; i <= b; i++) if (!out.includes(i - 1)) out.push(i - 1);
  }
  return out;
}

async function saveFile(url, body, successLabel) {
  setLoading('Preparing file…');
  try {
    const { blob, filename } = await postForFile(url, body);
    downloadBlob(blob, filename);
    toast(`${successLabel || 'Downloaded'} ${filename} (${fmtBytes(blob.size)})`, { type: 'success' });
  } finally {
    setLoading(false);
  }
}

// ------------------------------------------------------------------ download
let lastExport = { flatten: false, compress: 'none' };

export function downloadFilename() {
  const name = ($('#doc-filename').value || S.doc.filename || 'document.pdf').trim();
  return /\.pdf$/i.test(name) ? name : `${name}.pdf`;
}

export async function quickDownload() {
  if (!S.doc) return;
  emit('before-export');
  await saveFile(docUrl('/export'), { ...lastExport, password: '', filename: downloadFilename() });
  emit('saved');
}

export function downloadDialog({ focus } = {}) {
  emit('before-export');
  const name = input({ value: downloadFilename() });
  const flatten = check('Make annotations and form fields permanent (flatten)', lastExport.flatten);
  const compress = seg([
    { value: 'none', label: 'Original quality' },
    { value: 'balanced', label: 'Smaller' },
    { value: 'strong', label: 'Smallest' },
  ], lastExport.compress);
  const protect = check('Protect with a password', focus === 'password');
  const pw1 = input({ type: 'password', placeholder: 'Password', autocomplete: 'new-password' });
  const pw2 = input({ type: 'password', placeholder: 'Repeat password', autocomplete: 'new-password' });
  const allowPrint = check('Allow printing', true);
  const allowCopy = check('Allow copying text', true);
  const allowEdit = check('Allow editing', false);
  const pwBox = h('div', { class: 'field', hidden: focus !== 'password' }, h('div', { class: 'grid-2' }, pw1, pw2),
    h('div', { class: 'row', style: { flexWrap: 'wrap', gap: '14px' } }, allowPrint.el, allowCopy.el, allowEdit.el));
  protect.cb.addEventListener('change', () => { pwBox.hidden = !protect.cb.checked; if (protect.cb.checked) pw1.focus(); });
  const redactions = Object.values(S.doc.objects || {}).flat().filter((o) => o.type === 'redact').length;
  const body = [
    field('File name', name),
    h('div', { class: 'field' }, h('label', { text: 'File size' }), compress.el,
      h('div', { class: 'field-label', style: { fontWeight: 400, color: 'var(--text-3)' }, text: 'Smaller files re-compress large images. Text stays sharp.' })),
    flatten.el,
    protect.el,
    pwBox,
  ];
  if (redactions) body.push(h('div', { class: 'hint-box', html: `${icon('redact')} <b>${redactions}</b> redaction${redactions > 1 ? 's' : ''} will permanently remove the content underneath in this file.` }));
  const options = () => {
    if (protect.cb.checked) {
      if (!pw1.value) throw new Error('Enter a password.');
      if (pw1.value !== pw2.value) throw new Error("The passwords don't match.");
    }
    lastExport = { flatten: flatten.cb.checked, compress: compress.value };
    return {
      ...lastExport,
      filename: /\.pdf$/i.test(name.value.trim()) ? name.value.trim() : `${name.value.trim() || 'document'}.pdf`,
      password: protect.cb.checked ? pw1.value : '',
      permissions: { print: allowPrint.cb.checked, copy: allowCopy.cb.checked, modify: allowEdit.cb.checked, annotate: allowEdit.cb.checked },
    };
  };
  const actions = [{ label: 'Cancel', kind: 'ghost' }];
  if (S.doc.hasPath) {
    actions.push({ label: 'Save to original file', kind: 'secondary', icon: 'save', onClick: async () => {
      const opts = options();
      const res = await mutate('save', opts);
      toast(`Saved to ${res.saved}`, { type: 'success' });
    } });
  }
  actions.push({ label: 'Download', kind: 'primary', icon: 'download', onClick: async () => {
    const opts = options();
    $('#doc-filename').value = opts.filename;
    await saveFile(docUrl('/export'), opts);
    emit('saved');
  } });
  openDialog({ title: focus === 'password' ? 'Protect with password' : 'Download PDF', subtitle: 'Your edits are written into a new PDF file.', body, actions });
}

// ------------------------------------------------------------------ watermark
export function watermarkDialog() {
  const text = input({ value: 'CONFIDENTIAL' });
  const size = input({ type: 'number', value: 60, min: 6, max: 300 });
  const color = input({ type: 'color', value: '#dc2626', style: { padding: '2px', height: '32px' } });
  const opacity = h('input', { type: 'range', min: 0.05, max: 1, step: 0.05, value: 0.25 });
  const angle = input({ type: 'number', value: 45, min: -180, max: 180 });
  const mode = seg([{ value: 'center', label: 'Single' }, { value: 'tile', label: 'Tiled' }], 'center');
  const layer = seg([{ value: 'over', label: 'Over content' }, { value: 'under', label: 'Behind content' }], 'over');
  const pages = input({ placeholder: 'All pages (e.g. 1-3, 5)' });
  const preview = h('div', { class: 'preview-box' });
  const renderPreview = () => {
    preview.replaceChildren();
    const count = mode.value === 'tile' ? 9 : 1;
    const wrap = h('div', { style: { position: 'absolute', inset: '0', display: 'grid', placeItems: 'center', gridTemplateColumns: count > 1 ? 'repeat(3, 1fr)' : '1fr', overflow: 'hidden' } });
    for (let i = 0; i < count; i++) {
      wrap.append(h('div', { text: text.value, style: {
        transform: `rotate(${-angle.value}deg)`, color: color.value, opacity: opacity.value, fontWeight: 700,
        fontSize: `${clamp(size.value / (count > 1 ? 5 : 2.4), 6, 60)}px`, whiteSpace: 'nowrap', fontFamily: cssFont('Helvetica'),
      } }));
    }
    preview.append(wrap);
  };
  [text, size, color, opacity, angle].forEach((el) => el.addEventListener('input', renderPreview));
  mode.el.addEventListener('change', renderPreview);
  renderPreview();
  openDialog({
    title: 'Add watermark',
    body: [
      field('Text', text), preview,
      h('div', { class: 'grid-3' }, field('Size (pt)', size), field('Angle', angle), field('Colour', color)),
      field('Opacity', opacity),
      h('div', { class: 'grid-2' }, field('Layout', mode.el), field('Placement', layer.el)),
      field('Pages', pages),
    ],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Add watermark', kind: 'primary', onClick: async () => {
        await mutate('tools/watermark', { text: text.value, size: +size.value, color: color.value, opacity: +opacity.value, angle: +angle.value, mode: mode.value, layer: layer.value, pages: pages.value });
        toast('Watermark added', { type: 'success', action: () => emit('undo') });
      } },
    ],
  });
}

// ------------------------------------------------------------------ page numbers
export function pageNumbersDialog() {
  const presets = [
    ['{n}', '1, 2, 3'], ['Page {n}', 'Page 1'], ['Page {n} of {total}', 'Page 1 of N'], ['{n} / {total}', '1 / N'],
    ['{filename} — page {n}', 'File name — page 1'], ['{date}', "Today's date"], ['custom', 'Custom text…'],
  ];
  const preset = h('select', { class: 'select' }, presets.map(([v, l]) => h('option', { value: v, text: l })));
  preset.value = 'Page {n} of {total}';
  const custom = input({ value: 'Page {n} of {total}', hidden: true });
  preset.addEventListener('change', () => {
    custom.hidden = preset.value !== 'custom';
    if (preset.value !== 'custom') custom.value = preset.value;
    else custom.focus();
  });
  let position = 'bottom-center';
  const posGrid = h('div', { class: 'pos-grid' });
  const positions = ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'];
  positions.forEach((p, i) => {
    if (i === 3) posGrid.append(h('div', { class: 'mid' }));
    const b = h('button', { type: 'button', class: p === position ? 'on' : '', 'data-tip': p.replace('-', ' ') });
    b.addEventListener('click', () => {
      posGrid.querySelectorAll('button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      position = p;
    });
    posGrid.append(b);
  });
  const start = input({ type: 'number', value: 1, min: 0 });
  const size = input({ type: 'number', value: 10, min: 4, max: 72 });
  const color = input({ type: 'color', value: '#374151', style: { padding: '2px', height: '32px' } });
  const margin = input({ type: 'number', value: 28, min: 0, max: 200 });
  const pages = input({ placeholder: 'All pages (e.g. 2-)' });
  openDialog({
    title: 'Page numbers & headers',
    subtitle: 'Use {n} for the page number, {total} for the page count, {date} and {filename}.',
    body: [
      field('Format', preset), custom,
      h('div', { class: 'row', style: { alignItems: 'flex-start', gap: '20px' } },
        field('Position', posGrid),
        h('div', { class: 'grow', style: { display: 'grid', gap: '10px' } },
          h('div', { class: 'grid-2' }, field('Start at', start), field('Font size', size)),
          h('div', { class: 'grid-2' }, field('Margin (pt)', margin), field('Colour', color)))),
      field('Pages', pages),
    ],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Add', kind: 'primary', onClick: async () => {
        await mutate('tools/stamp', { format: custom.value, position, start: +start.value, size: +size.value, color: color.value, margin: +margin.value, pages: pages.value });
        toast('Page numbers added', { type: 'success', action: () => emit('undo') });
      } },
    ],
  });
}

// ------------------------------------------------------------------ properties
export function metadataDialog() {
  const m = S.doc.metadata;
  const fields = ['title', 'author', 'subject', 'keywords'].map((k) => [k, input({ value: m[k] || '' })]);
  openDialog({
    title: 'Document properties',
    body: [
      ...fields.map(([k, el]) => field(k[0].toUpperCase() + k.slice(1), el)),
      h('dl', { class: 'stat-list' },
        h('dt', { text: 'Pages' }), h('dd', { text: String(S.doc.pages.length) }),
        h('dt', { text: 'Page size' }), h('dd', { text: `${Math.round(S.doc.pages[0].w)} × ${Math.round(S.doc.pages[0].h)} pt (${(S.doc.pages[0].w / 72 * 25.4).toFixed(0)} × ${(S.doc.pages[0].h / 72 * 25.4).toFixed(0)} mm)` }),
        h('dt', { text: 'Created with' }), h('dd', { text: m.creator || '—' }),
        h('dt', { text: 'Producer' }), h('dd', { text: m.producer || '—' })),
    ],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Save', kind: 'primary', onClick: async () => {
        await mutate('tools/metadata', Object.fromEntries(fields.map(([k, el]) => [k, el.value])));
        toast('Properties updated', { type: 'success' });
      } },
    ],
  });
}

// ------------------------------------------------------------------ split / extract / images / text
export function splitDialog() {
  const mode = seg([{ value: 'every', label: 'Every N pages' }, { value: 'ranges', label: 'Custom ranges' }, { value: 'each', label: 'Each page' }], 'every');
  const every = input({ type: 'number', value: 1, min: 1 });
  const ranges = input({ placeholder: 'e.g. 1-3, 4-8, 9-' , hidden: true });
  const everyField = field('Pages per file', every);
  const rangesField = field('Ranges (one file per comma)', ranges);
  rangesField.hidden = true;
  mode.el.addEventListener('change', () => {
    everyField.hidden = mode.value !== 'every';
    rangesField.hidden = mode.value !== 'ranges';
    ranges.hidden = mode.value !== 'ranges';
  });
  openDialog({
    title: 'Split PDF',
    subtitle: `The ${S.doc.pages.length} pages are split into separate PDFs, downloaded as a ZIP.`,
    body: [mode.el, everyField, rangesField],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Split & download', kind: 'primary', icon: 'scissors', onClick: () => saveFile(docUrl('/split'), { mode: mode.value, value: mode.value === 'ranges' ? ranges.value : every.value }) },
    ],
  });
}

export function extractDialog() {
  const pages = input({ value: currentPageLabel() });
  openDialog({
    title: 'Extract pages',
    subtitle: 'Download selected pages as a new PDF (with your edits).',
    body: [field('Pages', pages, `For example 1-3, 7. This document has ${S.doc.pages.length} pages.`)],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Extract', kind: 'primary', icon: 'extract', onClick: () => {
        const idx = parsePageSpec(pages.value, S.doc.pages.length);
        return saveFile(docUrl('/extract'), { pageIds: idx.map((i) => S.doc.pages[i].id) });
      } },
    ],
  });
}

export function exportImagesDialog() {
  const pages = input({ placeholder: 'All pages' });
  const format = seg([{ value: 'png', label: 'PNG' }, { value: 'jpg', label: 'JPG' }], 'png');
  const dpi = h('select', { class: 'select' },
    h('option', { value: 72, text: 'Screen (72 dpi)' }), h('option', { value: 150, text: 'Standard (150 dpi)', selected: true }),
    h('option', { value: 300, text: 'Print (300 dpi)' }));
  openDialog({
    title: 'Export pages as images',
    body: [field('Pages', pages), h('div', { class: 'grid-2' }, field('Format', format.el), field('Resolution', dpi))],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Export', kind: 'primary', icon: 'image-down', onClick: () => {
        parsePageSpec(pages.value, S.doc.pages.length);
        return saveFile(docUrl('/export-images'), { pages: pages.value, format: format.value, dpi: +dpi.value });
      } },
    ],
  });
}

export function extractText() {
  return saveFile(docUrl('/extract-text'), {}).catch((err) => toast(err.message, { type: 'error' }));
}

export function extractImages() {
  return saveFile(docUrl('/extract-images'), {}).catch((err) => toast(err.message, { type: 'error' }));
}

// ------------------------------------------------------------------ OCR & crop
export function ocrDialog() {
  const langs = S.config.ocr.languages.length ? S.config.ocr.languages : ['eng'];
  const lang = h('select', { class: 'select' }, langs.map((l) => h('option', { value: l, text: l, selected: l === 'eng' })));
  const pages = input({ placeholder: 'All pages' });
  const force = check('Also process pages that already have text');
  openDialog({
    title: 'Recognize text (OCR)',
    subtitle: 'Makes scanned pages searchable, selectable and editable with Edit Text.',
    body: [h('div', { class: 'grid-2' }, field('Language', lang), field('Pages', pages)), force.el,
      h('div', { class: 'hint-box', text: 'This can take a few seconds per page. The page image is kept exactly as it is — an invisible text layer is added.' })],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Recognize text', kind: 'primary', icon: 'ocr', onClick: async () => {
        parsePageSpec(pages.value, S.doc.pages.length);
        setLoading('Recognizing text…');
        try {
          const res = await mutate('tools/ocr', { language: lang.value, pages: pages.value, force: force.cb.checked });
          if (res.ocr?.length) toast(`Recognized text on ${res.ocr.length} page${res.ocr.length > 1 ? 's' : ''}`, { type: 'success' });
          else toast('Those pages already contain text. Tick the option to process them anyway.');
        } finally {
          setLoading(false);
        }
      } },
    ],
  });
}

export function cropDialog() {
  const unit = seg([{ value: 'mm', label: 'mm' }, { value: 'pt', label: 'pt' }, { value: 'in', label: 'in' }], 'mm');
  const vals = ['top', 'right', 'bottom', 'left'].map((k) => [k, input({ type: 'number', value: 10, min: 0, step: 1 })]);
  const pages = input({ value: 'all' });
  const factor = () => ({ mm: 72 / 25.4, pt: 1, in: 72 }[unit.value]);
  openDialog({
    title: 'Crop pages',
    subtitle: 'Trim margins from the edges of pages.',
    body: [field('Unit', unit.el), h('div', { class: 'grid-2' }, ...vals.map(([k, el]) => field(k[0].toUpperCase() + k.slice(1), el))), field('Pages', pages)],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Crop', kind: 'primary', icon: 'crop', onClick: async () => {
        const idx = parsePageSpec(pages.value, S.doc.pages.length);
        const margins = Object.fromEntries(vals.map(([k, el]) => [k, (+el.value || 0) * factor()]));
        await mutate('pages/crop', { pageIds: idx.map((i) => S.doc.pages[i].id), margins });
        toast('Pages cropped', { type: 'success', action: () => emit('undo') });
      } },
    ],
  });
}

// ------------------------------------------------------------------ prompts
export function passwordPrompt(filename, wrong) {
  return new Promise((resolve) => {
    let done = false;
    const pw = input({ type: 'password', autocomplete: 'current-password', placeholder: 'Password' });
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    openDialog({
      title: 'Password required',
      subtitle: `"${filename}" is protected.`,
      body: [wrong ? h('div', { class: 'error-text', text: 'That password is incorrect. Try again.' }) : null, field('Password', pw)].filter(Boolean),
      actions: [
        { label: 'Cancel', kind: 'ghost', onClick: () => finish(null) },
        { label: 'Open', kind: 'primary', onClick: () => finish(pw.value) },
      ],
      onClose: () => finish(null),
    });
  });
}

export function confirmDialog({ title, message, confirmLabel = 'Continue', danger = false }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    openDialog({
      title,
      body: [h('p', { style: { margin: 0, color: 'var(--text-2)', lineHeight: 1.5 }, text: message })],
      actions: [
        { label: 'Cancel', kind: 'ghost', onClick: () => finish(false) },
        { label: confirmLabel, kind: danger ? 'danger' : 'primary', onClick: () => finish(true) },
      ],
      onClose: () => finish(false),
    });
  });
}

export function shortcutsDialog() {
  const rows = (items) => h('table', { class: 'shortcut-table' }, items.map(([label, keys]) => h('tr', {}, h('td', { text: label }), h('td', {}, ...keys.split(' ').map((k) => h('span', { class: 'kbd', text: k, style: { marginLeft: '3px' } }))))));
  openDialog({
    title: 'Keyboard shortcuts',
    wide: true,
    body: [h('div', { class: 'shortcut-cols' },
      rows([
        ['Select', 'V'], ['Edit text', 'E'], ['Add text', 'T'], ['Highlight', 'H'], ['Draw', 'D'], ['Shapes', 'S'],
        ['Image', 'I'], ['Signature', 'G'], ['Comment', 'N'], ['Checkmark', 'K'], ['Whiteout', 'W'], ['Redact', 'R'],
      ]),
      rows([
        ['Undo', `${K}Z`], ['Redo', `⇧${K}Z`], ['Download', `${K}S`], ['Open', `${K}O`], ['Find & replace', `${K}F`],
        ['Zoom in / out', `${K}+ ${K}−`], ['Fit width', `${K}0`], ['Duplicate', `${K}D`], ['Copy / paste', `${K}C ${K}V`],
        ['Delete selection', 'Del'], ['Nudge (×10 with ⇧)', '← ↑ → ↓'], ['Cancel / deselect', 'Esc'],
      ]))],
  });
}
