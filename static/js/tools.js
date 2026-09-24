// Tool registry and creation interactions.
import { S, emit, scale, pageById } from './state.js';
import { $, $$, h, clamp, toast, pickFiles } from './util.js';
import { icon } from './icons.js';
import { layerOf, pagePoint, pagesContainer } from './viewer.js';
import * as objects from './objects.js';
import * as textedit from './textedit.js';
import { postRaw, docUrl } from './api.js';

export const TOOLS = [
  { id: 'select', label: 'Select', icon: 'select', key: 'v', tip: 'Select, move and resize things you added (V)' },
  { id: 'edittext', label: 'Edit Text', icon: 'edittext', key: 'e', tip: 'Edit the text already in the PDF (E)' },
  { id: 'text', label: 'Add Text', icon: 'text', key: 't', tip: 'Add a text box (T)' },
  '|',
  { id: 'markup', label: 'Highlight', icon: 'highlight', key: 'h', tip: 'Highlight, underline or strike through text (H)' },
  { id: 'draw', label: 'Draw', icon: 'draw', key: 'd', tip: 'Pen and marker (D)' },
  { id: 'shape', label: 'Shapes', icon: 'shape', key: 's', tip: 'Rectangle, ellipse, line, arrow (S)' },
  '|',
  { id: 'image', label: 'Image', icon: 'image', key: 'i', tip: 'Insert an image (I)' },
  { id: 'signature', label: 'Sign', icon: 'signature', key: 'g', tip: 'Draw, type or upload a signature (G)' },
  { id: 'note', label: 'Comment', icon: 'note', key: 'n', tip: 'Sticky-note comment (N)' },
  { id: 'symbol', label: 'Checkmark', icon: 'symbol', key: 'k', tip: 'Stamp ✓ ✕ ● onto forms (K)' },
  '|',
  { id: 'whiteout', label: 'Whiteout', icon: 'whiteout', key: 'w', tip: 'Cover content with a solid box (W)' },
  { id: 'redact', label: 'Redact', icon: 'redact', key: 'r', tip: 'Permanently remove sensitive content (R)' },
];

export function toolById(id) {
  return TOOLS.find((t) => t !== '|' && t.id === id);
}

export function setTool(id) {
  if (S.placing) cancelPlacing();
  if (id === 'image') {
    startImagePlacement();
    return;
  }
  if (id === 'signature') {
    emit('open-signature');
    return;
  }
  if (S.editing?.kind === 'object') objects.finishEditing();
  S.tool = id;
  document.body.className = document.body.className.replace(/\btool-\S+/g, '').trim() + ` tool-${id}`;
  $$('.tool-btn').forEach((b) => b.classList.toggle('active', b.dataset.tool === id));
  if (!['select', 'text', 'edittext', 'note'].includes(id)) objects.clearSelection();
  if (id === 'markup') {
    for (const p of S.doc?.pages || []) textedit.getPageText(p.id).catch(() => {});
  }
  emit('tool', id);
}

// ------------------------------------------------------------------ dispatch
export function handlePointerDown(e) {
  const pageEl = e.target.closest('.page');
  if (!pageEl || e.button !== 0) return;
  if (S.placing) {
    placeAt(e, pageEl);
    return;
  }
  switch (S.tool) {
    case 'select':
    case 'edittext':
      objects.clearSelection();
      if (S.tool === 'select') startMarquee(e, pageEl);
      break;
    case 'text':
      objects.clearSelection();
      startTextBox(e, pageEl);
      break;
    case 'markup':
      startMarkup(e, pageEl);
      break;
    case 'draw':
      startInk(e, pageEl);
      break;
    case 'shape':
      startShape(e, pageEl);
      break;
    case 'whiteout':
    case 'redact':
      startBox(e, pageEl, S.tool);
      break;
    case 'symbol':
      placeSymbol(e, pageEl);
      break;
    case 'note':
      placeNote(e, pageEl);
      break;
    default:
  }
}

function track(e, onMove, onUp) {
  e.preventDefault();
  const move = (ev) => {
    const events = ev.getCoalescedEvents?.() || [ev];
    for (const ce of events.length ? events : [ev]) onMove(ce);
  };
  const up = (ev) => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', up);
    onUp(ev);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
}

function clampPoint(pageId, p) {
  const page = pageById(pageId);
  return { x: clamp(p.x, 0, page.w), y: clamp(p.y, 0, page.h) };
}

// ------------------------------------------------------------------ select marquee
function startMarquee(e, pageEl) {
  const pageId = pageEl.dataset.id;
  const draft = layerOf(pageId, 'draft');
  const s = scale();
  const p0 = pagePoint(pageEl, e.clientX, e.clientY);
  let box = null;
  let p1 = p0;
  track(e, (ev) => {
    p1 = pagePoint(pageEl, ev.clientX, ev.clientY);
    if (!box && Math.hypot(p1.x - p0.x, p1.y - p0.y) * s < 4) return;
    if (!box) {
      box = h('div', { class: 'marquee' });
      draft.append(box);
    }
    Object.assign(box.style, rectStyle(p0, p1, s));
  }, () => {
    if (!box) return;
    box.remove();
    const x0 = Math.min(p0.x, p1.x); const x1 = Math.max(p0.x, p1.x);
    const y0 = Math.min(p0.y, p1.y); const y1 = Math.max(p0.y, p1.y);
    const hits = (S.doc.objects?.[pageId] || []).filter((o) => {
      const b = objects.bounds(o);
      return b.x < x1 && b.x + b.w > x0 && b.y < y1 && b.y + b.h > y0;
    });
    if (hits.length) objects.select(pageId, hits.map((o) => o.id));
  });
}

function rectStyle(p0, p1, s) {
  return {
    left: `${Math.min(p0.x, p1.x) * s}px`,
    top: `${Math.min(p0.y, p1.y) * s}px`,
    width: `${Math.abs(p1.x - p0.x) * s}px`,
    height: `${Math.abs(p1.y - p0.y) * s}px`,
  };
}

// ------------------------------------------------------------------ text boxes
function startTextBox(e, pageEl) {
  const pageId = pageEl.dataset.id;
  const page = pageById(pageId);
  const s = scale();
  const size = S.prefs.text.size;
  const p0 = pagePoint(pageEl, e.clientX, e.clientY);
  const draft = layerOf(pageId, 'draft');
  let box = null;
  let p1 = p0;
  track(e, (ev) => {
    p1 = clampPoint(pageId, pagePoint(pageEl, ev.clientX, ev.clientY));
    if (!box && Math.abs(p1.x - p0.x) * s < 8) return;
    if (!box) {
      box = h('div', { class: 'draft-rect' });
      draft.append(box);
    }
    Object.assign(box.style, rectStyle({ x: p0.x, y: p0.y - size * 0.7 }, { x: p1.x, y: p0.y + size * 0.6 }, s));
  }, () => {
    box?.remove();
    const dragged = Math.abs(p1.x - p0.x) * s >= 8;
    const x = dragged ? Math.min(p0.x, p1.x) : p0.x - 2;
    const width = dragged ? Math.max(20, Math.abs(p1.x - p0.x)) : Math.min(260, Math.max(60, page.w - x - 12));
    const y = p0.y - size * (S.prefs.text.lineHeight || 1.25) * 0.5 - 2;
    objects.createTextBox(pageId, Math.max(0, x), Math.max(0, y), width);
  });
}

// ------------------------------------------------------------------ markup
function caretAt(data, p, snap = false) {
  let best = null;
  data.lines.forEach((line, li) => {
    const [x0, y0, x1, y1] = line.bbox;
    const inY = p.y >= y0 - 1 && p.y <= y1 + 1;
    const dy = inY ? 0 : Math.min(Math.abs(p.y - y0), Math.abs(p.y - y1));
    const dx = p.x < x0 ? x0 - p.x : p.x > x1 ? p.x - x1 : 0;
    const dist = dy * 3 + dx;
    if (!snap && (dy > 0 || dx > 4)) return;
    if (!best || dist < best.dist) best = { li, dist };
  });
  if (!best) return null;
  const line = data.lines[best.li];
  const xs = line.xs;
  let idx = 0;
  let bestD = Infinity;
  for (let i = 0; i < xs.length; i++) {
    const d = Math.abs(xs[i] - p.x);
    if (d < bestD) { bestD = d; idx = i; }
  }
  return { li: best.li, ci: idx };
}

function selectionRects(data, a, b) {
  let [s, t] = [a, b];
  if (a.li > b.li || (a.li === b.li && a.ci > b.ci)) [s, t] = [b, a];
  const rects = [];
  for (let li = s.li; li <= t.li; li++) {
    const line = data.lines[li];
    const n = line.text.length;
    let from = li === s.li ? s.ci : 0;
    let to = li === t.li ? t.ci : n;
    while (to > from && /\s/.test(line.text[to - 1])) to--;
    while (from < to && /\s/.test(line.text[from])) from++;
    if (to <= from) continue;
    rects.push([line.xs[from], line.bbox[1], line.xs[to], line.bbox[3]].map((v) => +v.toFixed(2)));
  }
  return rects;
}

function startMarkup(e, pageEl) {
  const pageId = pageEl.dataset.id;
  const cfg = S.prefs.markup;
  const color = cfg[cfg.kind];
  const draft = layerOf(pageId, 'draft');
  const s = scale();
  const p0 = pagePoint(pageEl, e.clientX, e.clientY);
  let data = null;
  let start = null;
  let rects = [];
  let freeform = false;
  const dataPromise = textedit.getPageText(pageId).then((d) => {
    data = d;
    start = d ? caretAt(d, p0) : null;
    freeform = !start;
  }).catch(() => { freeform = true; });
  let p1 = p0;
  const preview = () => {
    draft.replaceChildren();
    if (freeform) {
      rects = [[Math.min(p0.x, p1.x), Math.min(p0.y, p1.y), Math.max(p0.x, p1.x), Math.max(p0.y, p1.y)]];
    } else if (data && start) {
      const end = caretAt(data, p1, true) || start;
      rects = selectionRects(data, start, end);
    }
    for (const [x0, y0, x1, y1] of rects) {
      const r = h('div', { class: 'mk-draft' });
      Object.assign(r.style, {
        position: 'absolute', left: `${x0 * s}px`, top: `${y0 * s}px`, width: `${(x1 - x0) * s}px`, height: `${(y1 - y0) * s}px`,
        background: cfg.kind === 'highlight' ? color : 'rgba(79,70,229,.15)', opacity: cfg.kind === 'highlight' ? 0.45 : 1,
        mixBlendMode: 'multiply', borderBottom: cfg.kind === 'highlight' ? '' : `2px solid ${color}`,
      });
      draft.append(r);
    }
  };
  track(e, (ev) => {
    p1 = pagePoint(pageEl, ev.clientX, ev.clientY);
    preview();
  }, async () => {
    await dataPromise;
    preview();
    draft.replaceChildren();
    if (freeform) {
      const [x0, y0, x1, y1] = rects[0] || [0, 0, 0, 0];
      if ((x1 - x0) * s < 4 || (y1 - y0) * s < 4) {
        if (!data?.lines.length) toast('No selectable text here. Drag a box to highlight an area.');
        return;
      }
    }
    if (!rects.length) return;
    objects.addObject(pageId, { type: 'markup', kind: cfg.kind, rects, color, opacity: cfg.kind === 'highlight' ? cfg.opacity : 1 }, { selectIt: false, label: 'Highlight' });
  });
}

// ------------------------------------------------------------------ ink
function simplify(points, epsilon) {
  if (points.length < 3) return points;
  const sqSegDist = (p, a, b) => {
    let x = a[0]; let y = a[1];
    let dx = b[0] - x; let dy = b[1] - y;
    if (dx || dy) {
      const t = clamp(((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy), 0, 1);
      x += dx * t; y += dy * t;
    }
    dx = p[0] - x; dy = p[1] - y;
    return dx * dx + dy * dy;
  };
  const keep = new Uint8Array(points.length);
  keep[0] = keep[points.length - 1] = 1;
  const stack = [[0, points.length - 1]];
  const eps2 = epsilon * epsilon;
  while (stack.length) {
    const [first, last] = stack.pop();
    let maxD = 0; let idx = 0;
    for (let i = first + 1; i < last; i++) {
      const d = sqSegDist(points[i], points[first], points[last]);
      if (d > maxD) { maxD = d; idx = i; }
    }
    if (maxD > eps2) {
      keep[idx] = 1;
      stack.push([first, idx], [idx, last]);
    }
  }
  return points.filter((_, i) => keep[i]);
}

function draftSvg(pageId) {
  const page = pageById(pageId);
  const draft = layerOf(pageId, 'draft');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'draft-svg');
  svg.setAttribute('viewBox', `0 0 ${page.w} ${page.h}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  draft.append(svg);
  return svg;
}

function startInk(e, pageEl) {
  const pageId = pageEl.dataset.id;
  const mode = S.prefs.draw.mode;
  const cfg = S.prefs.draw[mode];
  const s = scale();
  const pts = [[pagePoint(pageEl, e.clientX, e.clientY).x, pagePoint(pageEl, e.clientX, e.clientY).y]];
  const svg = draftSvg(pageId);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  Object.entries({ fill: 'none', stroke: cfg.color, 'stroke-width': cfg.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-opacity': cfg.opacity }).forEach(([k, v]) => path.setAttribute(k, v));
  svg.append(path);
  const update = () => path.setAttribute('d', `M${pts.map((p) => `${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join('L')}`);
  update();
  track(e, (ev) => {
    const p = pagePoint(pageEl, ev.clientX, ev.clientY);
    const last = pts[pts.length - 1];
    if (Math.hypot(p.x - last[0], p.y - last[1]) * s < 1.2) return;
    pts.push([p.x, p.y]);
    update();
  }, () => {
    svg.remove();
    const simplified = simplify(pts, 0.3 / Math.max(0.5, S.zoom)).map(([x, y]) => [+x.toFixed(2), +y.toFixed(2)]);
    objects.addObject(pageId, { type: 'ink', paths: [simplified], stroke: cfg.color, strokeWidth: cfg.width, opacity: cfg.opacity, marker: mode === 'marker' }, { selectIt: false, label: 'Draw' });
  });
}

// ------------------------------------------------------------------ shapes & boxes
function startShape(e, pageEl) {
  const pageId = pageEl.dataset.id;
  const cfg = S.prefs.shape;
  const s = scale();
  const p0 = pagePoint(pageEl, e.clientX, e.clientY);
  const draft = layerOf(pageId, 'draft');
  let p1 = p0;
  let el = null;
  const lineKind = cfg.kind === 'line' || cfg.kind === 'arrow';
  const current = () => {
    let q = { ...p1 };
    if (lineKind) {
      return { type: cfg.kind, x1: p0.x, y1: p0.y, x2: q.x, y2: q.y, stroke: cfg.stroke || '#000000', strokeWidth: cfg.strokeWidth, opacity: cfg.opacity };
    }
    return { type: cfg.kind, x: Math.min(p0.x, q.x), y: Math.min(p0.y, q.y), w: Math.abs(q.x - p0.x), h: Math.abs(q.y - p0.y), stroke: cfg.stroke, strokeWidth: cfg.strokeWidth, fill: cfg.fill, opacity: cfg.opacity };
  };
  track(e, (ev) => {
    p1 = pagePoint(pageEl, ev.clientX, ev.clientY);
    if (ev.shiftKey) {
      if (lineKind) {
        const ang = Math.round(Math.atan2(p1.y - p0.y, p1.x - p0.x) / (Math.PI / 4)) * (Math.PI / 4);
        const len = Math.hypot(p1.x - p0.x, p1.y - p0.y);
        p1 = { x: p0.x + Math.cos(ang) * len, y: p0.y + Math.sin(ang) * len };
      } else {
        const d = Math.max(Math.abs(p1.x - p0.x), Math.abs(p1.y - p0.y));
        p1 = { x: p0.x + Math.sign(p1.x - p0.x || 1) * d, y: p0.y + Math.sign(p1.y - p0.y || 1) * d };
      }
    }
    const next = objects.renderObject({ id: 'draft', ...current() });
    next.style.pointerEvents = 'none';
    if (el) el.replaceWith(next); else draft.append(next);
    el = next;
  }, () => {
    el?.remove();
    let obj = current();
    const tiny = Math.hypot(p1.x - p0.x, p1.y - p0.y) * s < 4;
    if (tiny) {
      obj = lineKind
        ? { ...obj, x1: p0.x - 50, y1: p0.y, x2: p0.x + 50, y2: p0.y }
        : { ...obj, x: p0.x - 50, y: p0.y - 30, w: 100, h: 60 };
    }
    objects.addObject(pageId, obj, { selectIt: false, label: 'Add shape' });
  });
}

function startBox(e, pageEl, type) {
  const pageId = pageEl.dataset.id;
  const s = scale();
  const p0 = pagePoint(pageEl, e.clientX, e.clientY);
  const draft = layerOf(pageId, 'draft');
  const el = h('div', { class: type === 'redact' ? 'obj obj-redact' : 'draft-rect' });
  el.style.pointerEvents = 'none';
  if (type === 'whiteout') {
    el.style.background = S.prefs.whiteout.fill;
    el.style.borderStyle = 'dashed';
  }
  draft.append(el);
  let p1 = p0;
  track(e, (ev) => {
    p1 = clampPoint(pageId, pagePoint(pageEl, ev.clientX, ev.clientY));
    Object.assign(el.style, rectStyle(p0, p1, s));
  }, () => {
    el.remove();
    const w = Math.abs(p1.x - p0.x);
    const hh = Math.abs(p1.y - p0.y);
    if (w * s < 4 || hh * s < 4) return;
    const obj = { type, x: Math.min(p0.x, p1.x), y: Math.min(p0.y, p1.y), w, h: hh, fill: type === 'redact' ? S.prefs.redact.fill : S.prefs.whiteout.fill };
    objects.addObject(pageId, obj, { selectIt: false, label: type === 'redact' ? 'Mark redaction' : 'Whiteout' });
  });
}

function placeSymbol(e, pageEl) {
  e.preventDefault();
  const pageId = pageEl.dataset.id;
  const cfg = S.prefs.symbol;
  const p = pagePoint(pageEl, e.clientX, e.clientY);
  const size = cfg.size;
  objects.addObject(pageId, { type: 'symbol', kind: cfg.kind, x: p.x - size / 2, y: p.y - size / 2, w: size, h: size, color: cfg.color }, { selectIt: false, label: 'Stamp' });
}

function placeNote(e, pageEl) {
  e.preventDefault();
  const pageId = pageEl.dataset.id;
  const p = pagePoint(pageEl, e.clientX, e.clientY);
  const obj = { type: 'note', x: p.x - 4, y: p.y - 18 / scale(), text: '', color: S.prefs.note.color };
  objects.addObject(pageId, obj, { selectIt: true, label: 'Add comment' });
  setTool('select');
  objects.select(pageId, [obj.id]);
  setTimeout(() => objects.openNotePopover(pageId, obj.id), 0);
}

// ------------------------------------------------------------------ image placement
async function normalizeImage(file) {
  if (/^image\/(png|jpeg)$/.test(file.type)) return file;
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = reject;
      i.src = url;
    });
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth || 800;
    canvas.height = img.naturalHeight || 600;
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    return await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function uploadImage(blob) {
  const normalized = await normalizeImage(blob);
  return postRaw(docUrl('/assets'), normalized, { 'Content-Type': normalized.type || 'image/png' });
}

export async function startImagePlacement(file) {
  let f = file;
  if (!f) {
    [f] = await pickFiles({ accept: 'image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml' });
  }
  if (!f) return;
  try {
    const res = await uploadImage(f);
    beginPlacing({ asset: res.asset, w: res.w, h: res.h, kind: 'image' });
  } catch (err) {
    toast(err.message || 'Could not load that image.', { type: 'error' });
  }
}

let ghost = null;
let dismissHint = null;
export function beginPlacing(item) {
  if (S.editing?.kind === 'object') objects.finishEditing();
  S.placing = item;
  document.body.classList.add('placing');
  dismissHint?.();
  dismissHint = toast(`Click on a page to place the ${item.kind === 'signature' ? 'signature' : 'image'} · Esc to cancel`, { timeout: 6000 });
}

export function cancelPlacing() {
  S.placing = null;
  document.body.classList.remove('placing');
  ghost?.remove();
  ghost = null;
  dismissHint?.();
}

function placementSize(pageId) {
  const page = pageById(pageId);
  const item = S.placing;
  let w = item.w * 0.75;
  let hh = item.h * 0.75;
  const maxW = page.w * (item.kind === 'signature' ? 0.32 : 0.6);
  const maxH = page.h * 0.5;
  const k = Math.min(1, maxW / w, maxH / hh);
  w *= k;
  hh *= k;
  if (item.kind === 'signature' && w < 90) {
    hh *= 90 / w;
    w = 90;
  }
  return { w, h: hh };
}

export function handlePlacementMove(e) {
  if (!S.placing) return;
  const pageEl = e.target.closest?.('.page');
  if (!pageEl) {
    ghost?.remove();
    ghost = null;
    return;
  }
  const pageId = pageEl.dataset.id;
  const s = scale();
  const p = pagePoint(pageEl, e.clientX, e.clientY);
  const { w, h: hh } = placementSize(pageId);
  if (!ghost || ghost.parentElement !== layerOf(pageId, 'draft')) {
    ghost?.remove();
    ghost = h('div', { class: 'ghost-place' }, h('img', { src: docUrl(`/assets/${S.placing.asset}`), alt: '' }));
    layerOf(pageId, 'draft').append(ghost);
  }
  Object.assign(ghost.style, { left: `${(p.x - w / 2) * s}px`, top: `${(p.y - hh / 2) * s}px`, width: `${w * s}px`, height: `${hh * s}px` });
}

function placeAt(e, pageEl) {
  e.preventDefault();
  const pageId = pageEl.dataset.id;
  const p = pagePoint(pageEl, e.clientX, e.clientY);
  const { w, h: hh } = placementSize(pageId);
  const item = S.placing;
  cancelPlacing();
  const obj = { type: 'image', asset: item.asset, x: p.x - w / 2, y: p.y - hh / 2, w, h: hh, opacity: 1, signature: item.kind === 'signature' || undefined };
  setTool('select');
  objects.addObject(pageId, obj, { selectIt: true, label: item.kind === 'signature' ? 'Add signature' : 'Add image' });
}

export function initTools() {
  pagesContainer.addEventListener('pointermove', handlePlacementMove);
  pagesContainer.addEventListener('pointerover', textedit.handleHover);
}

export { icon };
