// Editing text that already exists in the PDF.
import { S, emit, on, scale, pageById, savePrefs } from './state.js';
import { mutate, getJSON, docUrl } from './api.js';
import { h, clamp, toast, isMac, setLoading } from './util.js';
import { icon } from './icons.js';
import { layerOf, pageElement, sampleBackground, whenPageImageLoaded, visiblePageIds } from './viewer.js';
import { cssFont } from './objects.js';

const textCache = new Map();
const fontFaces = new Map();
const measureCanvas = document.createElement('canvas');
let pendingOpen = null;

export function invalidateText() {
  textCache.clear();
}

export function getPageText(pageId) {
  const page = pageById(pageId);
  if (!page) return Promise.resolve(null);
  const key = `${S.doc.id}:${pageId}:${page.rev}`;
  if (!textCache.has(key)) {
    const p = getJSON(docUrl(`/page/${pageId}/text`)).then((r) => r.text);
    p.catch(() => textCache.delete(key));
    textCache.set(key, p);
  }
  return textCache.get(key);
}

function hashString(s) {
  let hsh = 0;
  for (let i = 0; i < s.length; i++) hsh = (Math.imul(31, hsh) + s.charCodeAt(i)) | 0;
  return (hsh >>> 0).toString(36);
}

/** Load the PDF's own embedded font into the browser for a faithful preview. */
function ensureFontFace(pageId, fontName) {
  if (!fontName || typeof FontFace === 'undefined') return Promise.resolve(null);
  const key = `${S.doc.id}:${fontName}`;
  if (!fontFaces.has(key)) {
    const family = `pdf-${hashString(key)}`;
    const url = docUrl(`/page/${pageId}/font?name=${encodeURIComponent(fontName)}`);
    const face = new FontFace(family, `url("${url}")`);
    fontFaces.set(key, face.load().then((f) => {
      document.fonts.add(f);
      return family;
    }).catch(() => null));
  }
  return fontFaces.get(key);
}

function fontStack(style, familyOverride) {
  if (familyOverride) return cssFont(familyOverride);
  const base = cssFont(style.css);
  const loaded = style._face;
  return loaded ? `"${loaded}", ${base}` : base;
}

// ------------------------------------------------------------------ hit boxes
export async function renderTextLayer(pageId) {
  const layer = layerOf(pageId, 'text');
  if (!layer) return;
  if (S.tool !== 'edittext') {
    layer.replaceChildren();
    layer._data = null;
    return;
  }
  const page = pageById(pageId);
  if (!page) return;
  const rev = page.rev;
  let data;
  try {
    data = await getPageText(pageId);
  } catch {
    return;
  }
  if (!data || S.tool !== 'edittext' || pageById(pageId)?.rev !== rev) return;
  const s = scale();
  const nodes = data.lines.map((line) => {
    const [x0, y0, x1, y1] = line.bbox;
    const el = h('div', { class: `text-hit${line.editable ? '' : ' readonly'}`, dataset: { line: line.id } });
    el.style.left = `${x0 * s - 1}px`;
    el.style.top = `${y0 * s}px`;
    el.style.width = `${(x1 - x0) * s + 2}px`;
    el.style.height = `${(y1 - y0) * s}px`;
    return el;
  });
  layer.replaceChildren(...nodes);
  layer._data = data;
  if (data.scanned) layer.append(scanBanner(pageId));
  // warm up embedded fonts used on this page
  data.styles.slice(0, 16).forEach((st) => {
    if (st.visible && st.embedded) ensureFontFace(pageId, st.font).then((fam) => { st._face = fam; });
  });
}

export function renderVisibleTextLayers() {
  for (const id of visiblePageIds()) renderTextLayer(id);
  if (S.tool !== 'edittext' && S.doc) {
    for (const p of S.doc.pages) {
      const layer = layerOf(p.id, 'text');
      if (layer?.childElementCount) layer.replaceChildren();
    }
  }
}

function scanBanner(pageId) {
  const page = S.doc.pages.findIndex((p) => p.id === pageId);
  const btn = h('button', { class: 'btn primary', text: 'Recognize text on this page' });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    setLoading('Recognizing text…');
    try {
      const res = await mutate('tools/ocr', { pages: String(page + 1), language: 'eng' });
      if (res.ocr?.length) toast('Text recognized. Click any line to edit it.', { type: 'success' });
    } catch {
      btn.disabled = false;
    } finally {
      setLoading(false);
    }
  });
  const available = S.config.ocr?.available;
  return h('div', { class: 'scan-banner' },
    h('strong', { text: 'This page is a scanned image' }),
    h('span', { text: available ? 'Recognize its text (OCR) to edit it. Edits are drawn in the font recognised from the scan.' : 'Install Tesseract (brew install tesseract) to recognise and edit its text.' }),
    available ? btn : null);
}

function paragraphOutline(pageId, para) {
  const layer = layerOf(pageId, 'text');
  layer.querySelector('.para-outline')?.remove();
  if (!para) return;
  const s = scale();
  const [x0, y0, x1, y1] = para.bbox;
  const el = h('div', { class: 'para-outline' });
  el.style.left = `${x0 * s - 4}px`;
  el.style.top = `${y0 * s - 3}px`;
  el.style.width = `${(x1 - x0) * s + 8}px`;
  el.style.height = `${(y1 - y0) * s + 6}px`;
  layer.append(el);
}

export function handleHover(e) {
  if (S.tool !== 'edittext' || S.editing || S.prefs.edittext.unit !== 'paragraph') return;
  const hit = e.target.closest?.('.text-hit');
  const pageEl = e.target.closest?.('.page');
  if (!pageEl) return;
  const layer = layerOf(pageEl.dataset.id, 'text');
  const data = layer?._data;
  if (!hit || !data) {
    layer?.querySelector('.para-outline')?.remove();
    return;
  }
  const line = data.lines.find((l) => l.id === hit.dataset.line);
  const para = line && data.paragraphs[line.para];
  paragraphOutline(pageEl.dataset.id, para && para.lines.length > 1 ? para : null);
}

// ------------------------------------------------------------------ editor
export function handlePointerDown(e) {
  if (S.tool !== 'edittext') return false;
  const inEditor = e.target.closest('.inline-editor, .edit-toolbar');
  if (inEditor) return true;
  const hit = e.target.closest('.text-hit');
  if (S.editing?.kind === 'text') {
    const wasEditing = S.editing;
    commitEditor();
    if (hit) pendingOpen = { pageId: wasEditing.pageId, clientX: e.clientX, clientY: e.clientY };
    e.preventDefault();
    return true;
  }
  if (!hit) return false;
  e.preventDefault();
  openAtHit(hit, e.clientX, e.clientY);
  return true;
}

function openAtHit(hit, clientX, clientY) {
  const pageEl = hit.closest('.page');
  const pageId = pageEl.dataset.id;
  const data = layerOf(pageId, 'text')?._data;
  const line = data?.lines.find((l) => l.id === hit.dataset.line);
  if (!line) return;
  if (!line.editable) {
    toast("Rotated or vertical text can't be edited in place. Use Whiteout + Add text instead.");
    return;
  }
  const para = data.paragraphs[line.para];
  if (S.prefs.edittext.unit === 'paragraph' && para && para.lines.length > 1) openEditor(pageId, data, { para }, clientX, clientY);
  else openEditor(pageId, data, { line }, clientX, clientY);
}

function metrics(fontCss, px) {
  const ctx = measureCanvas.getContext('2d');
  ctx.font = `${px}px ${fontCss}`;
  const m = ctx.measureText('Hgjy');
  return { ascent: m.fontBoundingBoxAscent ?? px * 0.8, descent: m.fontBoundingBoxDescent ?? px * 0.2 };
}

function openEditor(pageId, data, target, clientX, clientY) {
  const layer = layerOf(pageId, 'text');
  const s = scale();
  const isPara = Boolean(target.para);
  const lines = isPara ? target.para.lines.map((id) => data.lines.find((l) => l.id === id)) : [target.line];
  const first = lines[0];
  const style = data.styles[first.style];
  const origText = isPara ? target.para.text : first.text;
  const el = h('div', { class: `inline-editor${isPara ? ' paragraph' : ''}`, spellcheck: 'false' });
  const px = style.size * s;
  const bg = sampleBackground(pageId, isPara ? target.para.bbox : first.bbox);
  el.style.background = bg;
  el.style.fontSize = `${px}px`;

  if (isPara) {
    const para = target.para;
    const lineH = para.gap * s;
    el.textContent = origText;
    el.style.width = `${(para.right - para.left) * s + 3}px`;
    el.style.left = `${para.left * s}px`;
    el.style.lineHeight = `${lineH}px`;
    el.style.textAlign = para.align === 'justify' ? 'justify' : para.align;
    el.style.textIndent = `${para.indent * s}px`;
    el.style.minHeight = `${(para.bbox[3] - para.bbox[1]) * s}px`;
    const m = metrics(fontStack(style), px);
    const offset = (lineH - (m.ascent + m.descent)) / 2 + m.ascent;
    el.style.top = `${first.baseline * s - offset}px`;
    applyStyle(el, style);
  } else {
    const [x0, y0, x1, y1] = first.bbox;
    const lineH = Math.max((y1 - y0) * s, px * 1.05);
    el.style.left = `${x0 * s}px`;
    el.style.minWidth = `${(x1 - x0) * s + 4}px`;
    el.style.height = `${lineH}px`;
    el.style.lineHeight = `${lineH}px`;
    for (const [a, b, si] of first.runs) {
      const span = h('span', { text: first.text.slice(a, b) });
      applyStyle(span, data.styles[si], s, style);
      el.append(span);
    }
    const m = metrics(fontStack(style), px);
    const offset = (lineH - (m.ascent + m.descent)) / 2 + m.ascent;
    el.style.top = `${first.baseline * s - offset}px`;
    applyStyle(el, style);
  }
  try {
    el.contentEditable = 'plaintext-only';
  } catch {
    el.contentEditable = 'true';
  }
  if (el.contentEditable !== 'plaintext-only') el.contentEditable = 'true';
  layer.append(el);
  layer.querySelector('.para-outline')?.remove();

  const state = {
    kind: 'text', mode: isPara ? 'paragraph' : 'line', pageId, el, data, lines, style, origText,
    para: target.para || null, line: target.line || null, override: {}, dx: 0, dy: 0, toolbar: null,
  };
  S.editing = state;
  state.toolbar = buildToolbar(state);
  layer.append(state.toolbar);
  positionToolbar(state);
  if (!style.visible) applyScanMatch(state);

  el.focus({ preventScroll: true });
  placeCaret(el, clientX, clientY);

  const onKey = (e) => {
    e.stopPropagation();
    if (e.key === 'Escape') {
      e.preventDefault();
      commitEditor({ cancel: true });
    } else if (e.key === 'Enter' && (state.mode === 'line' || e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      commitEditor();
    }
  };
  const onPaste = (e) => {
    e.preventDefault();
    let text = e.clipboardData.getData('text/plain');
    if (state.mode === 'line') text = text.replace(/\s*\n\s*/g, ' ');
    document.execCommand('insertText', false, text);
  };
  const onInput = () => positionToolbar(state);
  el.addEventListener('keydown', onKey);
  el.addEventListener('paste', onPaste);
  el.addEventListener('input', onInput);
  state.cleanup = () => {
    el.removeEventListener('keydown', onKey);
    el.removeEventListener('paste', onPaste);
    el.removeEventListener('input', onInput);
  };
  emit('editing', state);
}

/** Text from OCR has no real font: ask the server which font the scan uses and preview with it. */
async function applyScanMatch(state) {
  const { el, toolbar } = state;
  const hint = toolbar.querySelector('.hint');
  const hintText = hint?.textContent;
  if (hint) hint.textContent = 'Matching font from the scan…';
  el.classList.add('matching');
  const query = state.mode === 'paragraph' ? `para=${state.para.id}` : `line=${encodeURIComponent(state.lines[0].id)}`;
  try {
    const { scan } = await getJSON(docUrl(`/page/${state.pageId}/scanfont?${query}`));
    if (S.editing !== state || !scan) return;
    state.scan = scan;
    const s = scale();
    for (const t of [el, ...el.querySelectorAll('span')]) {
      t.style.color = scan.color;
      if (!scan.matched) continue;
      t.style.fontFamily = cssFont(scan.family);
      t.style.fontWeight = scan.bold ? '700' : '400';
      t.style.fontStyle = 'normal';
      t.style.fontSize = `${scan.size * s}px`;
    }
    if (scan.matched && Array.isArray(scan.wordBold) && state.mode === 'line' && currentText(state) === state.origText) {
      // lines can mix weights (bold label + regular value): show each word in its own weight
      let index = 0;
      const parts = state.origText.split(/(\s+)/).filter((part) => part !== '').map((part) => {
        if (/^\s+$/.test(part)) return document.createTextNode(part);
        const span = h('span', { text: part });
        span.style.fontWeight = scan.wordBold[index++] ? '700' : '400';
        return span;
      });
      el.replaceChildren(...parts);
      for (const t of [el, ...el.querySelectorAll('span')]) {
        t.style.color = scan.color;
        t.style.fontFamily = cssFont(scan.family);
        t.style.fontStyle = 'normal';
        t.style.fontSize = `${scan.size * s}px`;
      }
      el.style.fontWeight = scan.bold ? '700' : '400';
    }
    if (scan.matched) {
      const px = scan.size * s;
      const m = metrics(cssFont(scan.family), px);
      let lineH = parseFloat(el.style.lineHeight) || px * 1.25;
      if (state.mode === 'line') {
        lineH = Math.max(px * 1.3, m.ascent + m.descent + 2);
        el.style.height = `${lineH}px`;
        el.style.lineHeight = `${lineH}px`;
      }
      el.style.top = `${scan.baseline * s - ((lineH - (m.ascent + m.descent)) / 2 + m.ascent)}px`;
      const original = toolbar.querySelector('select option[value=""]');
      if (original) original.textContent = `${scan.family}${scan.bold ? ' Bold' : ''} (matched to scan)`;
      const size = toolbar.querySelector('input[type=number]');
      if (size) size.value = (+scan.size).toFixed(1);
      toolbar.querySelector('[data-prop="bold"]')?.classList.toggle('active', Boolean(scan.bold));
      toolbar.querySelector('[data-prop="italic"]')?.classList.remove('active');
      const swatch = toolbar.querySelector('.color-chip span');
      if (swatch) swatch.style.background = scan.color;
      const colorInput = toolbar.querySelector('.color-chip input');
      if (colorInput) colorInput.value = scan.color;
    }
    positionToolbar(state);
  } catch {
    /* keep the generic preview */
  } finally {
    el.classList.remove('matching');
    if (hint && S.editing === state) hint.textContent = hintText;
  }
}

function applyStyle(el, style, s, parentStyle) {
  el.style.fontFamily = fontStack(style);
  el.style.color = style.visible ? style.color : '#111111';
  el.style.fontWeight = style.bold ? '700' : '400';
  el.style.fontStyle = style.italic ? 'italic' : 'normal';
  if (s && parentStyle && Math.abs(style.size - parentStyle.size) > 0.01) {
    el.style.fontSize = `${style.size * s}px`;
    el.style.verticalAlign = 'baseline';
  }
}

function placeCaret(el, clientX, clientY) {
  const sel = window.getSelection();
  let range = null;
  if (clientX !== undefined) {
    if (document.caretPositionFromPoint) {
      const pos = document.caretPositionFromPoint(clientX, clientY);
      if (pos && el.contains(pos.offsetNode)) {
        range = document.createRange();
        range.setStart(pos.offsetNode, pos.offset);
      }
    } else if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(clientX, clientY);
      if (r && el.contains(r.startContainer)) range = r;
    }
  }
  if (!range) {
    range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
  }
  sel.removeAllRanges();
  sel.addRange(range);
}

function positionToolbar(state) {
  const tb = state.toolbar;
  if (!tb) return;
  const el = state.el;
  const layer = el.parentElement;
  const top = el.offsetTop - tb.offsetHeight - 8;
  tb.style.top = `${top < 4 ? el.offsetTop + el.offsetHeight + 8 : top}px`;
  const maxLeft = Math.max(4, (layer?.clientWidth || 800) - tb.offsetWidth - 4);
  tb.style.left = `${clamp(el.offsetLeft, 4, maxLeft)}px`;
}

function currentText(state) {
  let text = state.el.innerText.replace(/ /g, ' ');
  if (state.mode === 'line') text = text.replace(/\n/g, ' ');
  else text = text.replace(/\n+$/, '');
  return text;
}

function refreshPreview(state) {
  const o = state.override;
  const s = scale();
  const targets = [state.el, ...state.el.querySelectorAll('span')];
  for (const t of targets) {
    if (o.family) t.style.fontFamily = cssFont(o.family);
    if (o.size) t.style.fontSize = `${o.size * s}px`;
    if (o.color) t.style.color = o.color;
    if (o.bold !== undefined) t.style.fontWeight = o.bold ? '700' : '400';
    if (o.italic !== undefined) t.style.fontStyle = o.italic ? 'italic' : 'normal';
  }
  if (o.size && state.mode === 'line') {
    const lh = Math.max(parseFloat(state.el.style.height), o.size * s * 1.25);
    state.el.style.height = `${lh}px`;
    state.el.style.lineHeight = `${lh}px`;
  }
  positionToolbar(state);
}

function buildToolbar(state) {
  const style = state.style;
  const tb = h('div', { class: 'edit-toolbar', onpointerdown: (e) => { if (!e.target.closest('select, input')) e.preventDefault(); } });
  const keepFocus = () => setTimeout(() => state.el.focus({ preventScroll: true }), 0);

  const fontSelect = h('select', { 'data-tip': 'Font' });
  const originalLabel = `${(style.font || 'Original').replace(/^[A-Z]{6}\+/, '').slice(0, 28)} (original)`;
  fontSelect.append(h('option', { value: '', text: originalLabel }));
  const common = S.config.fonts.filter((f) => f.common);
  const others = S.config.fonts.filter((f) => !f.common);
  const g1 = h('optgroup', { label: 'Common' }, common.map((f) => h('option', { value: f.family, text: f.family })));
  const g2 = h('optgroup', { label: 'All fonts' }, others.map((f) => h('option', { value: f.family, text: f.family })));
  fontSelect.append(g1, g2);
  fontSelect.addEventListener('change', () => {
    state.override.family = fontSelect.value || undefined;
    if (!fontSelect.value) {
      delete state.override.family;
      [state.el, ...state.el.querySelectorAll('span')].forEach((t) => { t.style.fontFamily = fontStack(style); });
    }
    refreshPreview(state);
    keepFocus();
  });

  const size = h('input', { type: 'number', min: 1, max: 400, step: 0.5, value: +style.size.toFixed(1), 'data-tip': 'Font size (pt)' });
  size.addEventListener('change', () => {
    const v = parseFloat(size.value);
    if (v > 0) {
      state.override.size = v;
      refreshPreview(state);
    }
  });
  size.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      size.dispatchEvent(new Event('change'));
      keepFocus();
    }
  });

  const toggle = (name, prop, tip) => {
    const b = h('button', { class: `icon-btn small${style[prop] ? ' active' : ''}`, html: icon(name), 'data-tip': tip, dataset: { prop } });
    b.addEventListener('click', () => {
      const base = state.scan?.matched ? (prop === 'bold' ? Boolean(state.scan.bold) : false) : style[prop];
      const cur = state.override[prop] !== undefined ? state.override[prop] : base;
      state.override[prop] = !cur;
      b.classList.toggle('active', !cur);
      refreshPreview(state);
      keepFocus();
    });
    return b;
  };

  const color = h('label', { class: 'color-chip', 'data-tip': 'Text colour' });
  const swatch = h('span');
  swatch.style.background = style.visible ? style.color : '#111111';
  const colorInput = h('input', { type: 'color', value: style.visible ? style.color : '#111111' });
  colorInput.addEventListener('input', () => {
    state.override.color = colorInput.value;
    swatch.style.background = colorInput.value;
    refreshPreview(state);
  });
  colorInput.addEventListener('change', keepFocus);
  color.append(swatch, colorInput);

  const moveHandle = h('button', { class: 'icon-btn small move-handle', html: icon('move'), 'data-tip': 'Drag to move' });
  moveHandle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const s = scale();
    const start = { x: e.clientX, y: e.clientY, dx: state.dx, dy: state.dy };
    const baseLeft = parseFloat(state.el.style.left);
    const baseTop = parseFloat(state.el.style.top);
    const startDx = state.dx * s;
    const startDy = state.dy * s;
    const move = (ev) => {
      state.dx = start.dx + (ev.clientX - start.x) / s;
      state.dy = start.dy + (ev.clientY - start.y) / s;
      state.el.style.left = `${baseLeft - startDx + state.dx * s}px`;
      state.el.style.top = `${baseTop - startDy + state.dy * s}px`;
      state.el.style.boxShadow = '0 0 0 1.5px #4f46e5, 0 6px 20px rgba(0,0,0,.18)';
      positionToolbar(state);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      keepFocus();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });

  const del = h('button', { class: 'icon-btn small', html: icon('trash'), 'data-tip': state.mode === 'line' ? 'Delete this line' : 'Delete this paragraph' });
  del.addEventListener('click', () => deleteEditorText(state));

  const done = h('button', { class: 'icon-btn small active', html: icon('check'), 'data-tip': 'Apply' });
  done.addEventListener('click', () => commitEditor());
  const cancel = h('button', { class: 'icon-btn small', html: icon('x'), 'data-tip': 'Cancel (Esc)' });
  cancel.addEventListener('click', () => commitEditor({ cancel: true }));

  const lineData = state.line;
  const para = lineData ? state.data.paragraphs[lineData.para] : state.para;
  const canPara = state.mode === 'paragraph' || (para && para.lines.length > 1);
  const paraBtn = h('button', {
    class: `icon-btn small${state.mode === 'paragraph' ? ' active' : ''}`,
    html: icon('paragraph'),
    'data-tip': state.mode === 'paragraph' ? 'Edit single lines instead' : 'Edit the whole paragraph (text reflows)',
    disabled: !canPara,
  });
  paraBtn.addEventListener('click', () => {
    if (currentText(state) !== state.origText) {
      toast('Apply or cancel your change first, then switch editing mode.');
      keepFocus();
      return;
    }
    const newUnit = state.mode === 'paragraph' ? 'line' : 'paragraph';
    S.prefs.edittext.unit = newUnit;
    savePrefs();
    emit('prefs');
    const { pageId, data } = state;
    const rect = state.el.getBoundingClientRect();
    commitEditor({ cancel: true });
    if (newUnit === 'paragraph') openEditor(pageId, data, { para }, rect.left + 2, rect.top + 4);
    else openEditor(pageId, data, { line: state.lines[0] }, rect.left + 2, rect.top + 4);
  });

  tb.append(fontSelect, size, h('div', { class: 'sep' }), toggle('bold', 'bold', 'Bold'), toggle('italic', 'italic', 'Italic'), color,
    h('div', { class: 'sep' }), paraBtn, moveHandle, del, h('div', { class: 'sep' }),
    h('span', { class: 'hint', text: state.mode === 'paragraph' ? `${isMac ? '⌘' : 'Ctrl+'}↵ apply` : '↵ apply · Esc cancel' }), cancel, done);
  return tb;
}

async function deleteEditorText(state) {
  finishUi(state);
  state.el.classList.add('saving');
  state.el.textContent = '';
  try {
    await mutate('text/delete', {
      pageId: state.pageId,
      items: state.lines.map((l) => ({ lineId: l.id, origText: l.text, bbox: l.bbox })),
    });
    await whenPageImageLoaded(state.pageId);
  } catch {
    /* toast shown by api */
  } finally {
    state.el.remove();
    renderTextLayer(state.pageId);
    emit('editing', null);
  }
}

function finishUi(state) {
  state.cleanup?.();
  state.toolbar?.remove();
  state.el.contentEditable = 'false';
  if (S.editing === state) S.editing = null;
}

export async function commitEditor({ cancel = false } = {}) {
  const state = S.editing;
  if (!state || state.kind !== 'text') return;
  finishUi(state);
  const text = currentText(state);
  const override = Object.fromEntries(Object.entries(state.override).filter(([, v]) => v !== undefined));
  const moved = Math.abs(state.dx) > 0.05 || Math.abs(state.dy) > 0.05;
  const changed = text !== state.origText || Object.keys(override).length || moved;
  if (cancel || !changed) {
    state.el.remove();
    emit('editing', null);
    openPending();
    return;
  }
  state.el.classList.add('saving');
  try {
    let result;
    if (state.mode === 'line') {
      const line = state.lines[0];
      result = await mutate('text/edit', {
        pageId: state.pageId,
        edits: [{ lineId: line.id, origText: line.text, text, style: override, dx: state.dx, dy: state.dy, bbox: line.bbox }],
      });
    } else {
      result = await mutate('text/paragraph', {
        pageId: state.pageId, paraId: state.para.id, lineIds: state.para.lines, origText: state.para.text,
        text, style: override, dx: state.dx, dy: state.dy,
      });
      if (result?.overflow > 2) toast('The paragraph got longer and may now overlap the content below it.');
    }
    if (result?.collateral) toast('Some nearby characters overlapped the edited text and may need checking.');
    await whenPageImageLoaded(state.pageId);
  } catch {
    /* error toast shown by api layer */
  } finally {
    state.el.remove();
    await renderTextLayer(state.pageId);
    emit('editing', null);
    openPending();
  }
}

function openPending() {
  if (!pendingOpen) return;
  const { clientX, clientY } = pendingOpen;
  pendingOpen = null;
  requestAnimationFrame(async () => {
    const target = document.elementFromPoint(clientX, clientY)?.closest('.text-hit');
    if (target && S.tool === 'edittext' && !S.editing) openAtHit(target, clientX, clientY);
  });
}

export function initTextEditing() {
  on('page-visible', (id) => { if (S.tool === 'edittext') renderTextLayer(id); });
  on('page-changed', (id) => { if (S.tool === 'edittext' && !(S.editing?.kind === 'text' && S.editing.pageId === id)) renderTextLayer(id); });
  on('zoom', () => {
    if (S.editing?.kind === 'text') commitEditor({ cancel: true });
    renderVisibleTextLayers();
  });
  on('tool', () => {
    if (S.editing?.kind === 'text' && S.tool !== 'edittext') commitEditor();
    document.body.classList.toggle('show-text-boxes', Boolean(S.prefs.edittext.showBoxes));
    renderVisibleTextLayers();
  });
}

export { pageElement };
