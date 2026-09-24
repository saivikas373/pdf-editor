// Right-hand properties panel: tool options and selection properties.
import { S, emit, on, savePrefs, objectsOf } from './state.js';
import { $, h, clamp, isMac, debounce } from './util.js';
import { icon } from './icons.js';
import * as objects from './objects.js';
import { toolById } from './tools.js';

const panel = $('#panel');
const K = isMac ? '⌘' : 'Ctrl+';

const TEXT_COLORS = ['#111827', '#4b5563', '#ffffff', '#dc2626', '#ea580c', '#ca8a04', '#16a34a', '#2563eb', '#7c3aed', '#db2777'];
const INK_COLORS = ['#111827', '#1d4ed8', '#dc2626', '#16a34a', '#ea580c', '#7c3aed', '#ffffff'];
const HIGHLIGHT_COLORS = ['#ffd400', '#7ee081', '#6ec6ff', '#ff8fb1', '#ffb057', '#c4a7ff'];
const LINE_COLORS = ['#e5484d', '#2563eb', '#16a34a', '#111827', '#f59e0b'];
const FILL_COLORS = ['#ffffff', '#fef3c7', '#dcfce7', '#dbeafe', '#fce7f3', '#e5e7eb', '#111827'];
const NOTE_COLORS = ['#ffc83d', '#7ee081', '#6ec6ff', '#ff8fb1', '#c4a7ff'];

// ------------------------------------------------------------------ controls
function head(iconName, title, desc) {
  return h('div', { class: 'panel-head' },
    h('div', { class: 'ph-icon', html: icon(iconName) }),
    h('div', {}, h('h2', { text: title }), desc ? h('p', { text: desc }) : null));
}

function section(title, ...children) {
  const cls = `panel-section${title === 'Tips' ? ' tips' : ''}`;  // tips are for mouse and keyboard
  return h('div', { class: cls }, title ? h('h3', { text: title }) : null, ...children);
}

function field(label, control, value) {
  return h('div', { class: 'field' },
    h('div', { class: 'field-label' }, h('span', { text: label }), value !== undefined ? h('span', { class: 'value', text: value }) : null),
    control);
}

function seg(options, value, onChange) {
  const el = h('div', { class: 'seg' });
  for (const opt of options) {
    const b = h('button', { class: opt.value === value ? 'on' : '', 'data-tip': opt.tip || null },
      opt.icon ? h('span', { html: icon(opt.icon) }) : null, opt.label ? h('span', { text: opt.label }) : null);
    b.addEventListener('click', () => {
      el.querySelectorAll('button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      onChange(opt.value);
    });
    el.append(b);
  }
  return el;
}

function swatches(colors, value, onChange, { none = false } = {}) {
  const el = h('div', { class: 'swatches' });
  const all = [];
  const mark = (v) => all.forEach(([b, c]) => b.classList.toggle('on', (c || null) === (v || null)));
  if (none) {
    const b = h('button', { class: 'swatch none', 'data-tip': 'None' });
    b.addEventListener('click', () => { mark(null); onChange(null); });
    all.push([b, null]);
    el.append(b);
  }
  for (const c of colors) {
    const b = h('button', { class: 'swatch', style: { background: c }, 'data-tip': c });
    b.addEventListener('click', () => { mark(c); onChange(c); });
    all.push([b, c]);
    el.append(b);
  }
  const custom = h('label', { class: 'swatch custom', 'data-tip': 'Custom colour' });
  const input = h('input', { type: 'color', value: value && value.startsWith('#') && value.length === 7 ? value : '#000000' });
  input.addEventListener('input', () => { mark(input.value); onChange(input.value, { live: true }); });
  input.addEventListener('change', () => onChange(input.value));
  custom.append(input);
  el.append(custom);
  mark(value);
  if (value && !colors.includes(value)) custom.classList.add('on');
  return el;
}

function slider({ min, max, step, value, format = (v) => v, onInput, onChange }) {
  const valueEl = h('span', { class: 'value', text: format(value) });
  const input = h('input', { type: 'range', min, max, step, value });
  input.addEventListener('input', () => {
    valueEl.textContent = format(+input.value);
    onInput?.(+input.value);
  });
  input.addEventListener('change', () => onChange?.(+input.value));
  return { input, valueEl };
}

function sliderField(label, opts) {
  const { input, valueEl } = slider(opts);
  return h('div', { class: 'field' }, h('div', { class: 'field-label' }, h('span', { text: label }), valueEl), input);
}

function fontSelect(value, onChange) {
  const sel = h('select', { class: 'select' });
  const fonts = S.config.fonts;
  const common = fonts.filter((f) => f.common);
  const others = fonts.filter((f) => !f.common);
  const known = fonts.some((f) => f.family === value);
  if (!known) sel.append(h('option', { value, text: value }));
  sel.append(h('optgroup', { label: 'Common' }, common.map((f) => h('option', { value: f.family, text: f.family, selected: f.family === value }))));
  sel.append(h('optgroup', { label: 'All fonts' }, others.map((f) => h('option', { value: f.family, text: f.family, selected: f.family === value }))));
  sel.value = value;
  sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}

function numberInput(value, { min = 1, max = 999, step = 1 } = {}, onChange) {
  const input = h('input', { class: 'input num', type: 'number', min, max, step, value: +(+value).toFixed(2) });
  input.addEventListener('change', () => {
    const v = parseFloat(input.value);
    if (!Number.isNaN(v)) onChange(clamp(v, min, max));
  });
  input.addEventListener('keydown', (e) => e.stopPropagation());
  return input;
}

function toggle(iconName, on, tip, onClick) {
  const b = h('button', { class: `toggle-btn${on ? ' on' : ''}`, html: icon(iconName), 'data-tip': tip });
  b.addEventListener('click', () => {
    const now = !b.classList.contains('on');
    b.classList.toggle('on', now);
    onClick(now);
  });
  return b;
}

function hint(html) {
  return h('div', { class: 'hint-box', html });
}

function setPref(group, key, value) {
  S.prefs[group][key] = value;
  savePrefs();
}

// ------------------------------------------------------------------ tool panels
function textControls(o, apply) {
  return [
    field('Font', fontSelect(o.font, (v) => apply({ font: v }))),
    h('div', { class: 'row' },
      h('div', { class: 'field grow' }, h('div', { class: 'field-label', text: 'Size' }), numberInput(o.size, { min: 2, max: 300, step: 0.5 }, (v) => apply({ size: v }))),
      h('div', { class: 'field' }, h('div', { class: 'field-label', text: 'Style' }),
        h('div', { class: 'toggles' },
          toggle('bold', o.bold, `Bold (${K}B)`, (v) => apply({ bold: v })),
          toggle('italic', o.italic, `Italic (${K}I)`, (v) => apply({ italic: v })),
          toggle('underline', o.underline, `Underline (${K}U)`, (v) => apply({ underline: v })),
          toggle('strike', o.strike, 'Strikethrough', (v) => apply({ strike: v }))))),
    field('Alignment', seg([
      { value: 'left', icon: 'align-left', tip: 'Left' },
      { value: 'center', icon: 'align-center', tip: 'Center' },
      { value: 'right', icon: 'align-right', tip: 'Right' },
    ], o.align || 'left', (v) => apply({ align: v }))),
    field('Colour', swatches(TEXT_COLORS, o.color, (v, meta) => apply({ color: v }, meta))),
    field('Background', swatches(FILL_COLORS, o.bg, (v, meta) => apply({ bg: v }, meta), { none: true })),
    sliderField('Line spacing', { min: 0.8, max: 2.5, step: 0.05, value: o.lineHeight || 1.25, format: (v) => v.toFixed(2), onInput: (v) => apply({ lineHeight: v }, { live: true }), onChange: (v) => apply({ lineHeight: v }) }),
    sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: o.opacity ?? 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => apply({ opacity: v }, { live: true }), onChange: (v) => apply({ opacity: v }) }),
  ];
}

function prefApplier(group, { alsoSelection = false } = {}) {
  return (patch, meta = {}) => {
    Object.assign(S.prefs[group], patch);
    if (!meta.live) savePrefs();
    if (alsoSelection) objects.patchSelected(patch, { commit: !meta.live });
  };
}

function toolPanel(tool) {
  const p = S.prefs;
  switch (tool) {
    case 'select': {
      const doc = S.doc;
      const count = Object.values(doc.objects || {}).reduce((n, l) => n + l.length, 0);
      return [
        head('select', 'Select', 'Click things you added to move, resize or restyle them.'),
        section('Document',
          h('dl', { class: 'stat-list' },
            h('dt', { text: 'Pages' }), h('dd', { text: String(doc.pages.length) }),
            h('dt', { text: 'Added items' }), h('dd', { text: String(count) }),
            doc.hasForms ? [h('dt', { text: 'Form fields' }), h('dd', { text: 'Yes — click to fill' })] : null,
            doc.metadata.title ? [h('dt', { text: 'Title' }), h('dd', { text: doc.metadata.title, title: doc.metadata.title })] : null),
          h('div', { class: 'btn-row' },
            h('button', { class: 'btn secondary', html: `${icon('edittext')}<span>Edit text</span>`, onclick: () => emit('set-tool', 'edittext') }),
            h('button', { class: 'btn secondary', html: `${icon('pages')}<span>Pages</span>`, onclick: () => emit('open-organizer') }))),
        section('Tips', hint(`Double-click a text box to edit it. Drag objects onto another page to move them there. Hold <kbd>Shift</kbd> to select several, arrow keys nudge, <kbd>${K}D</kbd> duplicates.`)),
      ];
    }
    case 'edittext':
      return [
        head('edittext', 'Edit Text', 'Click any text in the document to change it. Font, size and colour are matched automatically.'),
        section('Editing unit',
          seg([
            { value: 'line', label: 'Line', tip: 'Edit one line at a time; everything else stays exactly in place' },
            { value: 'paragraph', label: 'Paragraph', tip: 'Edit a whole paragraph; lines re-wrap automatically' },
          ], p.edittext.unit, (v) => { setPref('edittext', 'unit', v); emit('prefs'); }),
          h('label', { class: 'check-row' },
            h('input', { type: 'checkbox', checked: p.edittext.showBoxes, onchange: (e) => { setPref('edittext', 'showBoxes', e.target.checked); document.body.classList.toggle('show-text-boxes', e.target.checked); } }),
            h('span', { text: 'Outline all editable text' }))),
        section('How it works',
          hint('Press <kbd>Enter</kbd> to apply a line edit, <kbd>Esc</kbd> to cancel. Use the toolbar above the text to change font, size, colour or to move and delete it.'),
          hint('Text in a scanned page? Run <b>Tools → Recognize text (OCR)</b> first, then edit it here.'),
          h('button', { class: 'btn secondary', html: `${icon('replace')}<span>Find &amp; replace…</span>`, onclick: () => emit('open-search', { replace: true }) })),
      ];
    case 'text':
      return [
        head('text', 'Add Text', 'Click to place a text box, or drag to set its width.'),
        section('Text style', ...textControls(p.text, prefApplier('text'))),
      ];
    case 'markup': {
      const apply = (patch) => { Object.assign(p.markup, patch); savePrefs(); render(); };
      return [
        head('highlight', 'Highlight', 'Drag across text. On scanned pages, drag a box.'),
        section('Style',
          seg([
            { value: 'highlight', icon: 'highlight', tip: 'Highlight' },
            { value: 'underline', icon: 'underline', tip: 'Underline' },
            { value: 'strike', icon: 'strike', tip: 'Strikethrough' },
            { value: 'squiggly', icon: 'squiggly', tip: 'Squiggly underline' },
          ], p.markup.kind, (v) => apply({ kind: v })),
          field('Colour', swatches(p.markup.kind === 'highlight' ? HIGHLIGHT_COLORS : LINE_COLORS, p.markup[p.markup.kind], (v) => apply({ [p.markup.kind]: v }))),
          p.markup.kind === 'highlight'
            ? sliderField('Opacity', { min: 0.15, max: 1, step: 0.05, value: p.markup.opacity, format: (v) => `${Math.round(v * 100)}%`, onChange: (v) => apply({ opacity: v }) })
            : null),
      ];
    }
    case 'draw': {
      const mode = p.draw.mode;
      const cfg = p.draw[mode];
      const apply = (patch, meta = {}) => { Object.assign(cfg, patch); if (!meta.live) savePrefs(); };
      return [
        head('draw', 'Draw', 'Draw freehand on the page. Each stroke can be moved or deleted later.'),
        section('Brush',
          seg([
            { value: 'pen', label: 'Pen', icon: 'pen' },
            { value: 'marker', label: 'Marker', icon: 'highlight' },
          ], mode, (v) => { p.draw.mode = v; savePrefs(); render(); }),
          field('Colour', swatches(mode === 'marker' ? HIGHLIGHT_COLORS : INK_COLORS, cfg.color, (v, meta) => apply({ color: v }, meta))),
          sliderField('Thickness', { min: 0.5, max: mode === 'marker' ? 40 : 20, step: 0.5, value: cfg.width, format: (v) => `${v} pt`, onChange: (v) => apply({ width: v }) }),
          sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: cfg.opacity, format: (v) => `${Math.round(v * 100)}%`, onChange: (v) => apply({ opacity: v }) })),
      ];
    }
    case 'shape': {
      const cfg = p.shape;
      const apply = (patch, meta = {}) => { Object.assign(cfg, patch); if (!meta.live) savePrefs(); };
      const isLine = cfg.kind === 'line' || cfg.kind === 'arrow';
      return [
        head('shape', 'Shapes', 'Drag on the page. Hold Shift for squares, circles and straight angles.'),
        section('Shape',
          seg([
            { value: 'rect', icon: 'rect', tip: 'Rectangle' },
            { value: 'ellipse', icon: 'ellipse', tip: 'Ellipse' },
            { value: 'line', icon: 'line', tip: 'Line' },
            { value: 'arrow', icon: 'arrow', tip: 'Arrow' },
          ], cfg.kind, (v) => { cfg.kind = v; savePrefs(); render(); }),
          field('Stroke', swatches(LINE_COLORS, cfg.stroke, (v, meta) => apply({ stroke: v }, meta), { none: !isLine })),
          sliderField('Stroke width', { min: 0.5, max: 20, step: 0.5, value: cfg.strokeWidth, format: (v) => `${v} pt`, onChange: (v) => apply({ strokeWidth: v }) }),
          !isLine ? field('Fill', swatches(FILL_COLORS, cfg.fill, (v, meta) => apply({ fill: v }, meta), { none: true })) : null,
          sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: cfg.opacity, format: (v) => `${Math.round(v * 100)}%`, onChange: (v) => apply({ opacity: v }) })),
      ];
    }
    case 'note':
      return [
        head('note', 'Comment', 'Click anywhere to pin a sticky-note comment. It is saved as a standard PDF comment.'),
        section('Colour', swatches(NOTE_COLORS, p.note.color, (v) => setPref('note', 'color', v))),
      ];
    case 'symbol':
      return [
        head('symbol', 'Checkmark', 'Click to stamp marks onto forms and checkboxes.'),
        section('Mark',
          seg([
            { value: 'check', icon: 'check', label: 'Check' },
            { value: 'cross', icon: 'x', label: 'Cross' },
            { value: 'dot', icon: 'dot', label: 'Dot' },
          ], p.symbol.kind, (v) => setPref('symbol', 'kind', v)),
          field('Colour', swatches(['#111827', '#15803d', '#1d4ed8', '#dc2626'], p.symbol.color, (v) => setPref('symbol', 'color', v))),
          sliderField('Size', { min: 6, max: 48, step: 1, value: p.symbol.size, format: (v) => `${v} pt`, onChange: (v) => setPref('symbol', 'size', v) })),
      ];
    case 'whiteout':
      return [
        head('whiteout', 'Whiteout', 'Drag a box to cover content with a solid colour.'),
        section('Fill', swatches(['#ffffff', '#f5f5f4', '#fef9c3', '#000000'], p.whiteout.fill, (v) => setPref('whiteout', 'fill', v))),
        section(null, hint('Whiteout only hides content visually. To remove sensitive text or images permanently, use <b>Redact</b>.')),
      ];
    case 'redact': {
      const marks = Object.values(S.doc.objects || {}).flat().filter((o) => o.type === 'redact').length;
      const input = h('input', { class: 'input', placeholder: 'e.g. an account number or name', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') markBtn.click(); } });
      const markBtn = h('button', { class: 'btn secondary', html: `${icon('search')}<span>Mark all matches</span>`, onclick: () => emit('redact-search', input.value) });
      return [
        head('redact', 'Redact', 'Drag boxes over content to remove it permanently when you download.'),
        section('Find and mark', input, markBtn),
        section('Box colour', seg([{ value: '#000000', label: 'Black' }, { value: '#ffffff', label: 'White' }], p.redact.fill, (v) => setPref('redact', 'fill', v))),
        section(null,
          hint(`<b>${marks}</b> area${marks === 1 ? '' : 's'} marked. Text, images and drawings under the boxes are deleted from the downloaded file — this can't be undone after download.`),
          marks ? h('button', { class: 'btn ghost', html: `${icon('trash')}<span>Clear all marks</span>`, onclick: () => emit('clear-redactions') }) : null),
      ];
    }
    default:
      return [head(toolById(tool)?.icon || 'select', toolById(tool)?.label || '', '')];
  }
}

// ------------------------------------------------------------------ selection panel
const TYPE_NAMES = {
  text: ['text', 'Text box'], image: ['image', 'Image'], rect: ['rect', 'Rectangle'], ellipse: ['ellipse', 'Ellipse'],
  line: ['line', 'Line'], arrow: ['arrow', 'Arrow'], ink: ['draw', 'Drawing'], markup: ['highlight', 'Highlight'],
  note: ['note', 'Comment'], symbol: ['symbol', 'Mark'], whiteout: ['whiteout', 'Whiteout'], redact: ['redact', 'Redaction'],
};

function selectionPanel(objs) {
  const o = objs[0];
  const same = objs.every((x) => x.type === o.type);
  const [ic, name] = TYPE_NAMES[o.type] || ['select', 'Object'];
  const title = objs.length > 1 ? `${objs.length} items selected` : o.signature ? 'Signature' : name;
  const apply = (patch, meta = {}) => objects.patchSelected(patch, { commit: !meta.live });
  const parts = [head(ic, title, objs.length > 1 ? 'Drag to move them together.' : 'Drag to move, use the handles to resize.')];
  if (same) {
    switch (o.type) {
      case 'text':
        parts.push(section('Text style', ...textControls(o, (patch, meta = {}) => {
          apply(patch, meta);
          if (!meta.live) { Object.assign(S.prefs.text, patch); savePrefs(); }
        })));
        break;
      case 'image':
        parts.push(section('Appearance', sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: o.opacity ?? 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => apply({ opacity: v }, { live: true }), onChange: (v) => apply({ opacity: v }) })));
        break;
      case 'rect':
      case 'ellipse':
        parts.push(section('Appearance',
          field('Stroke', swatches(LINE_COLORS, o.stroke, (v, meta) => apply({ stroke: v }, meta), { none: true })),
          sliderField('Stroke width', { min: 0, max: 20, step: 0.5, value: o.strokeWidth ?? 2, format: (v) => `${v} pt`, onInput: (v) => apply({ strokeWidth: v }, { live: true }), onChange: (v) => apply({ strokeWidth: v }) }),
          field('Fill', swatches(FILL_COLORS, o.fill, (v, meta) => apply({ fill: v }, meta), { none: true })),
          sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: o.opacity ?? 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => apply({ opacity: v }, { live: true }), onChange: (v) => apply({ opacity: v }) })));
        break;
      case 'line':
      case 'arrow':
      case 'ink':
        parts.push(section('Appearance',
          o.type !== 'ink' ? field('Type', seg([{ value: 'line', icon: 'line', label: 'Line' }, { value: 'arrow', icon: 'arrow', label: 'Arrow' }], o.type, (v) => apply({ type: v }))) : null,
          field('Colour', swatches(o.marker ? HIGHLIGHT_COLORS : INK_COLORS, o.stroke, (v, meta) => apply({ stroke: v }, meta))),
          sliderField('Thickness', { min: 0.5, max: 40, step: 0.5, value: o.strokeWidth ?? 2, format: (v) => `${v} pt`, onInput: (v) => apply({ strokeWidth: v }, { live: true }), onChange: (v) => apply({ strokeWidth: v }) }),
          sliderField('Opacity', { min: 0.1, max: 1, step: 0.05, value: o.opacity ?? 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => apply({ opacity: v }, { live: true }), onChange: (v) => apply({ opacity: v }) })));
        break;
      case 'markup':
        parts.push(section('Appearance',
          seg([
            { value: 'highlight', icon: 'highlight', tip: 'Highlight' },
            { value: 'underline', icon: 'underline', tip: 'Underline' },
            { value: 'strike', icon: 'strike', tip: 'Strikethrough' },
            { value: 'squiggly', icon: 'squiggly', tip: 'Squiggly' },
          ], o.kind, (v) => apply({ kind: v, opacity: v === 'highlight' ? 0.5 : 1 })),
          field('Colour', swatches(o.kind === 'highlight' ? HIGHLIGHT_COLORS : LINE_COLORS, o.color, (v) => apply({ color: v })))));
        break;
      case 'note':
        parts.push(section('Comment',
          h('div', { class: 'hint-box', text: o.text || 'Empty comment' }),
          field('Colour', swatches(NOTE_COLORS, o.color, (v) => apply({ color: v }))),
          h('button', { class: 'btn secondary', html: `${icon('note')}<span>Edit comment</span>`, onclick: () => objects.openNotePopover(S.selection.pageId, o.id) })));
        break;
      case 'symbol':
        parts.push(section('Mark',
          seg([{ value: 'check', icon: 'check' }, { value: 'cross', icon: 'x' }, { value: 'dot', icon: 'dot' }], o.kind, (v) => apply({ kind: v })),
          field('Colour', swatches(['#111827', '#15803d', '#1d4ed8', '#dc2626'], o.color, (v) => apply({ color: v })))));
        break;
      case 'whiteout':
        parts.push(section('Fill', swatches(['#ffffff', '#f5f5f4', '#fef9c3', '#000000'], o.fill, (v) => apply({ fill: v }))));
        break;
      case 'redact':
        parts.push(section(null, hint('Everything under this box is removed from the downloaded PDF.')));
        break;
      default:
    }
  }
  if (objs.length === 1 && ['text', 'image', 'rect', 'ellipse', 'whiteout', 'redact', 'symbol'].includes(o.type)) {
    const b = objects.bounds(o);
    const set = (k) => (v) => {
      const cur = objects.selectedObjects()[0];
      if (cur) objects.patchSelected({ [k]: v });
    };
    parts.push(section('Position (pt)',
      h('div', { class: 'grid-2' },
        field('X', numberInput(b.x, { min: -2000, max: 20000, step: 1 }, set('x'))),
        field('Y', numberInput(b.y, { min: -2000, max: 20000, step: 1 }, set('y'))),
        field('Width', numberInput(b.w, { min: 1, max: 20000, step: 1 }, set('w'))),
        o.type !== 'text' ? field('Height', numberInput(b.h, { min: 1, max: 20000, step: 1 }, set('h'))) : null)));
  }
  parts.push(section(null, h('div', { class: 'btn-row' },
    h('button', { class: 'btn secondary', html: `${icon('copy')}<span>Duplicate</span>`, onclick: objects.duplicateSelected }),
    h('button', { class: 'btn secondary', html: `${icon('trash')}<span>Delete</span>`, onclick: objects.deleteSelected }))));
  return parts;
}

// ------------------------------------------------------------------ render
export function render() {
  if (!S.doc) {
    panel.replaceChildren();
    return;
  }
  const sel = objects.selectedObjects();
  const parts = sel.length && S.editing?.kind !== 'text' ? selectionPanel(sel) : toolPanel(S.tool);
  panel.replaceChildren(...parts.flat().filter(Boolean));
}

const renderSoon = debounce(() => {
  if (panel.contains(document.activeElement) && document.activeElement.tagName !== 'BUTTON') return;
  render();
}, 60);

export function initPanel() {
  on('tool', render);
  on('selection', render);
  on('prefs', render);
  on('doc', ({ settled }) => { if (settled) renderSoon(); });
}

export { objectsOf };
