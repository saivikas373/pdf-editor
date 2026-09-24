// Overlay objects: rendering, selection, move / resize, text boxes.
import { S, emit, on, scale, objectsOf, pageById } from './state.js';
import { mutate, docUrl } from './api.js';
import { $$, h, clamp, uid, rgba, isMac } from './util.js';
import { icon } from './icons.js';
import { layerOf, pageElement, pagePoint, pageAtPoint, pagesContainer } from './viewer.js';

const BOX_TYPES = new Set(['text', 'image', 'rect', 'ellipse', 'whiteout', 'redact', 'symbol']);
const NOTE_SIZE = 22; // css px, independent of zoom

// Local, not-yet-committed text box (created by the text tool, saved on first blur).
let pendingNew = null; // { pageId, obj }
let clipboard = null;

// ------------------------------------------------------------------ geometry
export function bounds(o) {
  switch (o.type) {
    case 'line':
    case 'arrow':
      return { x: Math.min(o.x1, o.x2), y: Math.min(o.y1, o.y2), w: Math.abs(o.x2 - o.x1), h: Math.abs(o.y2 - o.y1) };
    case 'ink': {
      const pts = o.paths.flat();
      if (!pts.length) return { x: 0, y: 0, w: 0, h: 0 };
      const xs = pts.map((p) => p[0]);
      const ys = pts.map((p) => p[1]);
      const x = Math.min(...xs);
      const y = Math.min(...ys);
      return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
    }
    case 'markup': {
      const rs = o.rects || [];
      const x = Math.min(...rs.map((r) => r[0]));
      const y = Math.min(...rs.map((r) => r[1]));
      return { x, y, w: Math.max(...rs.map((r) => r[2])) - x, h: Math.max(...rs.map((r) => r[3])) - y };
    }
    case 'note': {
      const size = NOTE_SIZE / scale();
      return { x: o.x, y: o.y, w: size, h: size };
    }
    default:
      return { x: o.x, y: o.y, w: o.w, h: o.h };
  }
}

export function translated(o, dx, dy) {
  const n = structuredClone(o);
  switch (o.type) {
    case 'line':
    case 'arrow':
      n.x1 += dx; n.x2 += dx; n.y1 += dy; n.y2 += dy;
      break;
    case 'ink':
      n.paths = o.paths.map((path) => path.map(([x, y]) => [x + dx, y + dy]));
      break;
    case 'markup':
      n.rects = o.rects.map(([a, b, c, d]) => [a + dx, b + dy, c + dx, d + dy]);
      break;
    default:
      n.x += dx; n.y += dy;
  }
  return n;
}

function resized(o, b0, b1) {
  const n = structuredClone(o);
  const sx = b0.w ? b1.w / b0.w : 1;
  const sy = b0.h ? b1.h / b0.h : 1;
  const mapX = (x) => b1.x + (x - b0.x) * sx;
  const mapY = (y) => b1.y + (y - b0.y) * sy;
  if (o.type === 'ink') {
    n.paths = o.paths.map((path) => path.map(([x, y]) => [mapX(x), mapY(y)]));
  } else if (o.type === 'line' || o.type === 'arrow') {
    n.x1 = mapX(o.x1); n.x2 = mapX(o.x2); n.y1 = mapY(o.y1); n.y2 = mapY(o.y2);
  } else {
    n.x = b1.x; n.y = b1.y; n.w = b1.w;
    if (o.type !== 'text') n.h = b1.h;
  }
  return n;
}

// ------------------------------------------------------------------ fonts
export function cssFont(family) {
  const f = (family || 'Helvetica').replace(/"/g, '');
  const key = f.toLowerCase();
  const generic = /courier|mono|menlo|consol/.test(key) ? 'monospace' : /times|georgia|garamond|palatino|baskerville|didot|serif|cambria|book/.test(key) ? 'serif' : 'sans-serif';
  return `"${f}", ${generic}`;
}

// ------------------------------------------------------------------ rendering
export function objectById(pageId, id) {
  if (pendingNew && pendingNew.pageId === pageId && pendingNew.obj.id === id) return pendingNew.obj;
  return objectsOf(pageId).find((o) => o.id === id) || null;
}

function allObjects(pageId) {
  const list = objectsOf(pageId).slice();
  if (pendingNew && pendingNew.pageId === pageId) list.push(pendingNew.obj);
  return list;
}

function svgEl(markup, b, pad) {
  return `<svg viewBox="${b.x - pad} ${b.y - pad} ${b.w + 2 * pad} ${b.h + 2 * pad}" preserveAspectRatio="none">${markup}</svg>`;
}

export function smoothPath(pts) {
  if (!pts.length) return '';
  const f = (v) => v.toFixed(2);
  if (pts.length === 1) return `M${f(pts[0][0])} ${f(pts[0][1])}l0.01 0.01`;
  if (pts.length === 2) return `M${f(pts[0][0])} ${f(pts[0][1])}L${f(pts[1][0])} ${f(pts[1][1])}`;
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const mx = (pts[i][0] + pts[i + 1][0]) / 2;
    const my = (pts[i][1] + pts[i + 1][1]) / 2;
    d += `Q${f(pts[i][0])} ${f(pts[i][1])} ${f(mx)} ${f(my)}`;
  }
  const last = pts[pts.length - 1];
  return `${d}L${f(last[0])} ${f(last[1])}`;
}

export function arrowHead(o) {
  const dx = o.x2 - o.x1;
  const dy = o.y2 - o.y1;
  const len = Math.hypot(dx, dy);
  if (len < 0.1) return null;
  const ux = dx / len;
  const uy = dy / len;
  const head = Math.max(8, (o.strokeWidth || 2) * 4);
  const bx = o.x2 - ux * head;
  const by = o.y2 - uy * head;
  const px = -uy * head * 0.5;
  const py = ux * head * 0.5;
  return { points: `${o.x2},${o.y2} ${bx + px},${by + py} ${bx - px},${by - py}`, endX: o.x2 - ux * head * 0.8, endY: o.y2 - uy * head * 0.8 };
}

export function symbolMarkup(kind, b, color) {
  const lw = Math.max(1, Math.min(b.w, b.h) * 0.12);
  const P = (fx, fy) => `${(b.x + fx * b.w).toFixed(2)},${(b.y + fy * b.h).toFixed(2)}`;
  const common = `stroke="${color}" stroke-width="${lw}" stroke-linecap="round" stroke-linejoin="round" fill="none"`;
  if (kind === 'cross') return `<path d="M${P(0.18, 0.18)}L${P(0.82, 0.82)}M${P(0.82, 0.18)}L${P(0.18, 0.82)}" ${common}/>`;
  if (kind === 'dot') return `<circle cx="${b.x + b.w / 2}" cy="${b.y + b.h / 2}" r="${Math.min(b.w, b.h) * 0.25}" fill="${color}"/>`;
  return `<polyline points="${P(0.12, 0.55)} ${P(0.4, 0.82)} ${P(0.9, 0.18)}" ${common}/>`;
}

function applyTextStyle(el, o, s) {
  el.style.fontFamily = cssFont(o.font);
  el.style.fontSize = `${o.size * s}px`;
  el.style.lineHeight = String(o.lineHeight || 1.25);
  el.style.color = o.color || '#000';
  el.style.fontWeight = o.bold ? '700' : '400';
  el.style.fontStyle = o.italic ? 'italic' : 'normal';
  el.style.textDecoration = [o.underline && 'underline', o.strike && 'line-through'].filter(Boolean).join(' ') || 'none';
  el.style.textAlign = o.align || 'left';
  el.style.background = o.bg || 'transparent';
  el.style.padding = `${(o.padding ?? 2) * s}px`;
  el.style.opacity = o.opacity ?? 1;
  el.style.width = `${o.w * s}px`;
  el.style.fontKerning = 'none';
  el.style.fontVariantLigatures = 'none';
  el.style.textUnderlineOffset = `${o.size * s * 0.12}px`;
}

export function renderObject(o) {
  const s = scale();
  const b = bounds(o);
  const el = h('div', { class: `obj obj-${o.type}`, dataset: { id: o.id } });
  const place = (bb = b) => {
    el.style.left = `${bb.x * s}px`;
    el.style.top = `${bb.y * s}px`;
    el.style.width = `${bb.w * s}px`;
    el.style.height = `${bb.h * s}px`;
  };
  switch (o.type) {
    case 'text': {
      el.style.left = `${o.x * s}px`;
      el.style.top = `${o.y * s}px`;
      applyTextStyle(el, o, s);
      el.textContent = o.text || '';
      el.dataset.placeholder = 'Type here';
      el.classList.add('placeholder');
      break;
    }
    case 'image':
      place();
      el.style.opacity = o.opacity ?? 1;
      el.append(h('img', { src: docUrl(`/assets/${o.asset}`), draggable: 'false', alt: '' }));
      break;
    case 'rect':
    case 'ellipse':
      place();
      el.classList.add('obj-box');
      el.style.borderWidth = o.stroke ? `${(o.strokeWidth || 0) * s}px` : '0';
      el.style.borderColor = o.stroke || 'transparent';
      el.style.background = o.fill || 'transparent';
      el.style.opacity = o.opacity ?? 1;
      if (o.type === 'ellipse') el.style.borderRadius = '50%';
      break;
    case 'whiteout':
      place();
      el.style.background = o.fill || '#ffffff';
      break;
    case 'redact':
      place();
      break;
    case 'line':
    case 'arrow':
    case 'ink': {
      const pad = (o.strokeWidth || 2) * 2 + (o.type === 'arrow' ? 12 : 0) + 2;
      el.classList.add('obj-svg');
      el.style.left = `${(b.x - pad) * s}px`;
      el.style.top = `${(b.y - pad) * s}px`;
      el.style.width = `${(b.w + 2 * pad) * s}px`;
      el.style.height = `${(b.h + 2 * pad) * s}px`;
      el.style.opacity = o.opacity ?? 1;
      const stroke = o.stroke || '#000';
      const sw = o.strokeWidth || 2;
      let markup;
      if (o.type === 'ink') {
        markup = o.paths.map((p) => `<path d="${smoothPath(p)}" fill="none" stroke="${stroke}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"/>`).join('');
      } else {
        const head = o.type === 'arrow' ? arrowHead(o) : null;
        const ex = head ? head.endX : o.x2;
        const ey = head ? head.endY : o.y2;
        markup = `<path d="M${o.x1} ${o.y1}L${ex} ${ey}" stroke="${stroke}" stroke-width="${sw}" stroke-linecap="round"/>`;
        if (head) markup += `<polygon points="${head.points}" fill="${stroke}" stroke="${stroke}" stroke-width="0.5" stroke-linejoin="round"/>`;
        // wide invisible hit area for thin lines
        markup += `<path d="M${o.x1} ${o.y1}L${o.x2} ${o.y2}" stroke="transparent" stroke-width="${Math.max(10 / s, sw)}"/>`;
      }
      el.innerHTML = svgEl(markup, b, pad);
      break;
    }
    case 'symbol':
      place();
      el.classList.add('obj-svg');
      el.style.opacity = o.opacity ?? 1;
      el.innerHTML = svgEl(symbolMarkup(o.kind, b, o.color || '#000'), b, 0);
      break;
    case 'markup': {
      place();
      const color = o.color || '#ffd400';
      const op = o.opacity ?? 0.5;
      for (const [x0, y0, x1, y1] of o.rects || []) {
        const r = h('div', { class: `mk ${o.kind}` });
        const hgt = y1 - y0;
        r.style.left = `${(x0 - b.x) * s}px`;
        r.style.width = `${(x1 - x0) * s}px`;
        if (o.kind === 'highlight') {
          r.style.top = `${(y0 - b.y) * s}px`;
          r.style.height = `${hgt * s}px`;
          r.style.background = rgba(color, Math.min(1, op + 0.15));
        } else {
          const thick = Math.max(1, hgt * 0.07 * s);
          const yy = o.kind === 'strike' ? y0 + hgt * 0.52 : y1 - hgt * 0.1;
          r.style.top = `${(yy - b.y) * s - thick / 2}px`;
          r.style.height = `${o.kind === 'squiggly' ? thick * 3 : thick}px`;
          if (o.kind === 'squiggly') {
            const w = thick * 4;
            r.style.background = `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${thick * 3}'><path d='M0 ${thick * 2.2} Q${w / 4} 0 ${w / 2} ${thick * 1.5} T${w} ${thick * 1.5}' fill='none' stroke='${color}' stroke-width='${thick * 0.8}'/></svg>`)}") repeat-x`;
          } else {
            r.style.background = color;
          }
        }
        el.append(r);
      }
      break;
    }
    case 'note':
      el.style.left = `${o.x * s}px`;
      el.style.top = `${o.y * s}px`;
      el.style.background = o.color || '#ffc83d';
      el.innerHTML = icon('note');
      el.title = o.text || '';
      break;
    default:
      place();
  }
  return el;
}

export function renderPageObjects(pageId) {
  const layer = layerOf(pageId, 'objects');
  if (!layer) return;
  const editingEl = S.editing?.kind === 'object' && S.editing.pageId === pageId ? S.editing.el : null;
  const children = [];
  for (const o of allObjects(pageId)) {
    if (editingEl && o.id === S.editing.id) children.push(editingEl);
    else {
      const el = renderObject(o);
      if (o.type === 'text' && o.text) el.classList.remove('placeholder');
      children.push(el);
    }
  }
  layer.replaceChildren(...children);
  if (S.selection?.pageId === pageId) renderSelection();
}

export function renderAllObjects() {
  if (!S.doc) return;
  for (const page of S.doc.pages) renderPageObjects(page.id);
  renderSelection();
}

// ------------------------------------------------------------------ selection
export function selectedObjects() {
  if (!S.selection) return [];
  return S.selection.ids.map((id) => objectById(S.selection.pageId, id)).filter(Boolean);
}

export function select(pageId, ids, { toggle = false } = {}) {
  if (toggle && S.selection?.pageId === pageId) {
    const set = new Set(S.selection.ids);
    for (const id of ids) set.has(id) ? set.delete(id) : set.add(id);
    ids = [...set];
  }
  S.selection = ids.length ? { pageId, ids } : null;
  closeNotePopover();
  renderSelection();
  emit('selection', S.selection);
}

export function clearSelection() {
  if (!S.selection) return;
  S.selection = null;
  renderSelection();
  emit('selection', null);
}

function handlesFor(o) {
  switch (o.type) {
    case 'text': return ['w', 'e'];
    case 'image':
    case 'symbol': return ['nw', 'ne', 'se', 'sw'];
    case 'line':
    case 'arrow': return ['p1', 'p2'];
    case 'markup':
    case 'note': return [];
    default: return ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
  }
}

function elementBounds(pageId, o) {
  // text boxes grow with their content: use the rendered height
  const b = bounds(o);
  if (o.type === 'text') {
    const el = layerOf(pageId, 'objects')?.querySelector(`.obj[data-id="${o.id}"]`);
    if (el) b.h = el.offsetHeight / scale();
  }
  return b;
}

export function renderSelection() {
  $$('.sel-box, .obj-actions').forEach((el) => el.remove());
  const objs = selectedObjects();
  if (!objs.length) return;
  const pageId = S.selection.pageId;
  const layer = layerOf(pageId, 'objects');
  if (!layer) return;
  const s = scale();
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  for (const o of objs) {
    const b = elementBounds(pageId, o);
    x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.w); y1 = Math.max(y1, b.y + b.h);
  }
  const box = h('div', { class: 'sel-box' });
  const single = objs.length === 1 ? objs[0] : null;
  const pad = single?.type === 'note' ? 0 : 0;
  box.style.left = `${x0 * s - pad}px`;
  box.style.top = `${y0 * s - pad}px`;
  box.style.width = `${(x1 - x0) * s + 2 * pad}px`;
  box.style.height = `${(y1 - y0) * s + 2 * pad}px`;
  if (single?.type === 'note') {
    box.style.width = `${NOTE_SIZE}px`;
    box.style.height = `${NOTE_SIZE}px`;
  }
  if (single && !(S.editing?.kind === 'object')) {
    for (const name of handlesFor(single)) {
      const hd = h('div', { class: `handle ${name.startsWith('p') ? 'pt' : name}`, dataset: { handle: name } });
      if (name === 'p1' || name === 'p2') {
        const px = (name === 'p1' ? single.x1 : single.x2) * s - x0 * s;
        const py = (name === 'p1' ? single.y1 : single.y2) * s - y0 * s;
        hd.style.left = `${px}px`;
        hd.style.top = `${py}px`;
      }
      box.append(hd);
    }
  }
  if (single?.type === 'line' || single?.type === 'arrow') box.style.outline = 'none';
  layer.append(box);
  if (S.editing?.kind === 'object') return;

  const actions = h('div', { class: 'obj-actions' });
  const btn = (name, tip, fn) => h('button', { class: 'icon-btn', 'data-tip': tip, html: icon(name), onpointerdown: (e) => e.stopPropagation(), onclick: fn });
  if (single?.type === 'text') actions.append(btn('text', 'Edit text (double-click)', () => editTextObject(pageId, single.id, { caret: 'end' })));
  if (single?.type === 'note') actions.append(btn('note', 'Open note', () => openNotePopover(pageId, single.id)));
  if (single?.type !== 'markup') actions.append(btn('copy', `Duplicate (${isMac ? '⌘' : 'Ctrl+'}D)`, duplicateSelected));
  actions.append(btn('front', 'Bring to front', () => arrange('front')), btn('back', 'Send to back', () => arrange('back')));
  actions.append(btn('trash', 'Delete (Del)', deleteSelected));
  actions.style.left = `${x0 * s}px`;
  actions.style.top = `${Math.max(0, y0 * s - 46)}px`;
  if (y0 * s - 46 < 0) actions.style.top = `${y1 * s + 10}px`;
  layer.append(actions);
}

// ------------------------------------------------------------------ mutations
function localSet(pageId, objs) {
  if (!S.doc.objects) S.doc.objects = {};
  S.doc.objects[pageId] = objs;
}

export function addObject(pageId, obj, { selectIt = true, label } = {}) {
  obj.id = obj.id || uid();
  localSet(pageId, [...objectsOf(pageId), obj]);
  renderPageObjects(pageId);
  if (selectIt) select(pageId, [obj.id]);
  return mutate('objects', { ops: [{ op: 'add', pageId, object: obj, label }] }).then(() => obj);
}

export function updateObjects(pageId, updated, { label, render = true } = {}) {
  const byId = new Map(updated.map((o) => [o.id, o]));
  localSet(pageId, objectsOf(pageId).map((o) => byId.get(o.id) || o));
  if (render) renderPageObjects(pageId);
  return mutate('objects', { ops: updated.map((o) => ({ op: 'update', pageId, object: o, label })) });
}

export function deleteObjects(pageId, ids) {
  const set = new Set(ids);
  localSet(pageId, objectsOf(pageId).filter((o) => !set.has(o.id)));
  if (S.selection?.pageId === pageId) S.selection = null;
  renderPageObjects(pageId);
  emit('selection', S.selection);
  return mutate('objects', { ops: [{ op: 'delete', pageId, ids }] });
}

export function deleteSelected() {
  if (!S.selection) return;
  if (S.editing?.kind === 'object') return;
  deleteObjects(S.selection.pageId, S.selection.ids);
}

export function duplicateSelected() {
  const objs = selectedObjects();
  if (!objs.length) return;
  const pageId = S.selection.pageId;
  const copies = objs.map((o) => ({ ...translated(o, 12, 12), id: uid() }));
  localSet(pageId, [...objectsOf(pageId), ...copies]);
  renderPageObjects(pageId);
  select(pageId, copies.map((c) => c.id));
  mutate('objects', { ops: copies.map((c) => ({ op: 'add', pageId, object: c, label: 'Duplicate' })) });
}

export function copySelected() {
  const objs = selectedObjects();
  if (objs.length) clipboard = structuredClone(objs);
  return objs.length > 0;
}

export function paste(pageId) {
  if (!clipboard?.length || !pageId) return false;
  const copies = clipboard.map((o) => ({ ...translated(o, 14, 14), id: uid() }));
  clipboard = copies.map((c) => structuredClone(c));
  localSet(pageId, [...objectsOf(pageId), ...copies]);
  renderPageObjects(pageId);
  select(pageId, copies.map((c) => c.id));
  mutate('objects', { ops: copies.map((c) => ({ op: 'add', pageId, object: c, label: 'Paste' })) });
  return true;
}

export function arrange(where) {
  if (!S.selection) return;
  const { pageId, ids } = S.selection;
  const set = new Set(ids);
  const list = objectsOf(pageId);
  const picked = list.filter((o) => set.has(o.id));
  const rest = list.filter((o) => !set.has(o.id));
  localSet(pageId, where === 'front' ? [...rest, ...picked] : [...picked, ...rest]);
  renderPageObjects(pageId);
  mutate('objects', { ops: [{ op: where, pageId, ids }] });
}

export function nudgeSelected(dx, dy) {
  const objs = selectedObjects();
  if (!objs.length) return;
  const moved = objs.map((o) => translated(o, dx, dy));
  scheduleNudge(S.selection.pageId, moved);
}

let nudgeTimer = null;
let nudgePending = null;
function scheduleNudge(pageId, moved) {
  const byId = new Map(moved.map((o) => [o.id, o]));
  localSet(pageId, objectsOf(pageId).map((o) => byId.get(o.id) || o));
  renderPageObjects(pageId);
  nudgePending = { pageId, ids: moved.map((o) => o.id) };
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => {
    const { pageId: pid, ids } = nudgePending;
    const objs = ids.map((id) => objectById(pid, id)).filter(Boolean);
    mutate('objects', { ops: objs.map((o) => ({ op: 'update', pageId: pid, object: o, label: 'Move' })) });
    nudgePending = null;
  }, 400);
}

/** Apply a property patch to the selection (from the properties panel). */
export function patchSelected(patch, { commit = true } = {}) {
  // commit typed text first, otherwise the patch would work on a stale copy
  if (S.editing?.kind === 'object') finishEditing();
  const objs = selectedObjects();
  if (!objs.length) return;
  const pageId = S.selection.pageId;
  const ids = new Set(objs.map((o) => o.id));
  const applyPatch = (o) => {
    const n = { ...o };
    for (const [k, v] of Object.entries(patch)) {
      if (k === 'color' && ['rect', 'ellipse', 'line', 'arrow', 'ink'].includes(o.type)) n.stroke = v;
      else n[k] = v;
    }
    return n;
  };
  localSet(pageId, objectsOf(pageId).map((o) => (ids.has(o.id) ? applyPatch(o) : o)));
  renderPageObjects(pageId);
  if (!commit) return;
  requestAnimationFrame(() => {
    // re-read the latest state: other edits may have landed since the patch was made
    const latest = [...ids].map((id) => objectById(pageId, id)).filter(Boolean);
    const final = latest.map((o) => (o.type === 'text' ? withMeasuredLayout(pageId, o) : o));
    if (final.length) updateObjects(pageId, final, { label: 'Change style', render: false });
  });
}

// ------------------------------------------------------------------ text layout
const measureCanvas = document.createElement('canvas');

/** Measure where the browser broke lines and placed baselines, so the PDF matches the screen. */
export function measureTextLayout(el, o) {
  const s = scale();
  const elRect = el.getBoundingClientRect();
  const fontPx = o.size * s;
  const ctx = measureCanvas.getContext('2d');
  ctx.font = `${o.italic ? 'italic ' : ''}${o.bold ? '700' : '400'} ${fontPx}px ${cssFont(o.font)}`;
  const metrics = ctx.measureText('Hg');
  const ascent = metrics.fontBoundingBoxAscent ?? fontPx * 0.8;
  const lines = [];
  let current = null;
  const range = document.createRange();
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.data;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === '\n') {
        current = null;
        continue;
      }
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const rects = range.getClientRects();
      if (!rects.length) continue;
      const r = rects[rects.length - 1];
      if (!current || Math.abs(r.top - current.top) > fontPx * 0.5) {
        current = { top: r.top, left: r.left, text: '' };
        lines.push(current);
      }
      current.text += ch;
    }
  }
  return {
    lines: lines
      .filter((l) => l.text.trim())
      .map((l) => ({
        text: l.text,
        x: +((l.left - elRect.left) / s).toFixed(2),
        baseline: +((l.top - elRect.top + ascent) / s).toFixed(2),
      })),
  };
}

function withMeasuredLayout(pageId, o) {
  const el = layerOf(pageId, 'objects')?.querySelector(`.obj[data-id="${o.id}"]`);
  if (!el) return o;
  const n = { ...o, layout: measureTextLayout(el, o) };
  n.h = +(el.offsetHeight / scale()).toFixed(2);
  return n;
}

// ------------------------------------------------------------------ text box editing
export function createTextBox(pageId, x, y, width) {
  if (S.editing) return;
  const p = S.prefs.text;
  const obj = {
    id: uid(), type: 'text', x, y, w: width || 220, h: p.size * p.lineHeight + 4,
    text: '', font: p.font, size: p.size, color: p.color, bold: p.bold, italic: p.italic,
    underline: p.underline, strike: p.strike, align: p.align, bg: p.bg, lineHeight: p.lineHeight, opacity: p.opacity, padding: 2,
  };
  pendingNew = { pageId, obj };
  renderPageObjects(pageId);
  select(pageId, [obj.id]);
  editTextObject(pageId, obj.id, { caret: 'end' });
}

export function editTextObject(pageId, id, { caret = 'end', clientX, clientY } = {}) {
  const layer = layerOf(pageId, 'objects');
  const el = layer?.querySelector(`.obj[data-id="${id}"]`);
  if (!el) return;
  if (S.editing) finishEditing();
  S.editing = { kind: 'object', pageId, id, el, original: el.textContent };
  try {
    el.contentEditable = 'plaintext-only';
  } catch {
    el.contentEditable = 'true';
  }
  if (el.contentEditable !== 'plaintext-only') el.contentEditable = 'true';
  el.classList.add('editing');
  el.spellcheck = true;
  renderSelection();
  el.focus({ preventScroll: true });
  const sel = window.getSelection();
  const range = document.createRange();
  let placed = false;
  if (clientX !== undefined) {
    const pos = document.caretPositionFromPoint?.(clientX, clientY);
    const r = pos ? (() => { const rr = document.createRange(); rr.setStart(pos.offsetNode, pos.offset); return rr; })() : document.caretRangeFromPoint?.(clientX, clientY);
    if (r && el.contains(r.startContainer)) {
      range.setStart(r.startContainer, r.startOffset);
      placed = true;
    }
  }
  if (!placed) {
    range.selectNodeContents(el);
    if (caret !== 'all') range.collapse(false);
  }
  sel.removeAllRanges();
  sel.addRange(range);
  const onInput = () => {
    el.classList.toggle('placeholder', !el.textContent);
    renderSelection();
  };
  const onKey = (e) => {
    if (e.key === 'Escape' || (e.key === 'Enter' && mod(e))) {
      e.preventDefault();
      finishEditing();
    }
    e.stopPropagation();
  };
  const onPaste = (e) => {
    if (el.contentEditable === 'plaintext-only') return;
    e.preventDefault();
    document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
  };
  const onBlur = () => setTimeout(() => {
    if (S.editing?.el === el && document.activeElement !== el) finishEditing();
  }, 0);
  el.addEventListener('input', onInput);
  el.addEventListener('keydown', onKey);
  el.addEventListener('paste', onPaste);
  el.addEventListener('blur', onBlur);
  S.editing.cleanup = () => {
    el.removeEventListener('input', onInput);
    el.removeEventListener('keydown', onKey);
    el.removeEventListener('paste', onPaste);
    el.removeEventListener('blur', onBlur);
  };
  emit('editing', S.editing);
}

function mod(e) {
  return isMac ? e.metaKey : e.ctrlKey;
}

export function finishEditing() {
  const ed = S.editing;
  if (!ed || ed.kind !== 'object') return;
  ed.cleanup?.();
  const { el, pageId, id } = ed;
  el.contentEditable = 'false';
  el.classList.remove('editing');
  S.editing = null;
  const text = el.innerText.replace(/\n$/, '');
  const isNew = pendingNew && pendingNew.obj.id === id;
  const obj = objectById(pageId, id);
  if (!obj) {
    renderPageObjects(pageId);
    return;
  }
  if (!text.trim()) {
    if (isNew) {
      pendingNew = null;
      if (S.selection?.ids.includes(id)) S.selection = null;
      renderPageObjects(pageId);
      emit('selection', S.selection);
    } else {
      deleteObjects(pageId, [id]);
    }
    emit('editing', null);
    return;
  }
  const updated = { ...obj, text };
  updated.layout = measureTextLayout(el, updated);
  updated.h = +(el.offsetHeight / scale()).toFixed(2);
  if (isNew) {
    pendingNew = null;
    localSet(pageId, [...objectsOf(pageId), updated]);
    renderPageObjects(pageId);
    mutate('objects', { ops: [{ op: 'add', pageId, object: updated, label: 'Add text' }] });
  } else if (text !== obj.text) {
    updateObjects(pageId, [updated], { label: 'Edit text box' });
  } else {
    renderPageObjects(pageId);
  }
  renderSelection();
  emit('editing', null);
}

/** Re-measure text boxes after fonts load or zoom changes (layout is zoom independent in pt). */
export function refreshTextLayouts() {
  /* layouts are measured at commit time; nothing to do continuously */
}

// ------------------------------------------------------------------ notes
let notePop = null;
export function closeNotePopover() {
  if (notePop) {
    notePop.commit();
    notePop.el.remove();
    notePop = null;
  }
}

export function openNotePopover(pageId, id) {
  closeNotePopover();
  const o = objectById(pageId, id);
  const layer = layerOf(pageId, 'objects');
  if (!o || !layer) return;
  const s = scale();
  const textarea = h('textarea', { placeholder: 'Write a comment…' });
  textarea.value = o.text || '';
  const el = h('div', { class: 'note-pop', onpointerdown: (e) => e.stopPropagation() },
    textarea,
    h('div', { class: 'note-actions' },
      h('button', { class: 'icon-btn small', html: icon('trash'), 'data-tip': 'Delete note', onclick: () => { notePop = null; el.remove(); deleteObjects(pageId, [id]); } }),
      h('button', { class: 'btn small', text: 'Done', onclick: () => closeNotePopover() })));
  el.style.left = `${Math.min(o.x * s + NOTE_SIZE + 6, layer.clientWidth - 240)}px`;
  el.style.top = `${o.y * s}px`;
  layer.append(el);
  textarea.addEventListener('keydown', (e) => e.stopPropagation());
  notePop = {
    el,
    commit: () => {
      const cur = objectById(pageId, id);
      if (cur && textarea.value !== (cur.text || '')) updateObjects(pageId, [{ ...cur, text: textarea.value }], { label: 'Edit note' });
    },
  };
  setTimeout(() => textarea.focus(), 0);
}

// ------------------------------------------------------------------ pointer interaction
export function objectsInteractive() {
  return ['select', 'text', 'edittext', 'note'].includes(S.tool) && !S.placing;
}

/** Returns true when the event was handled here (object hit / handle). */
export function handlePointerDown(e) {
  const pageEl = e.target.closest('.page');
  if (!pageEl || e.button !== 0) return false;
  const pageId = pageEl.dataset.id;
  if (S.editing?.kind === 'object') {
    if (S.editing.el.contains(e.target)) return true;
    // the first click outside a text box just finishes editing it
    finishEditing();
    if (!e.target.closest('.obj')) return true;
  }
  if (e.target.closest('.note-pop, .obj-actions')) return true;
  const handle = e.target.closest('.handle');
  if (handle && S.selection?.pageId === pageId) {
    startResize(e, pageEl, handle.dataset.handle);
    return true;
  }
  const objEl = e.target.closest('.obj');
  if (!objEl || !objectsInteractive()) {
    closeNotePopover();
    return false;
  }
  const id = objEl.dataset.id;
  const o = objectById(pageId, id);
  if (!o) return false;
  if (S.tool === 'note' && o.type !== 'note') return false;
  const alreadySelected = S.selection?.pageId === pageId && S.selection.ids.includes(id);
  if (e.shiftKey) {
    select(pageId, [id], { toggle: true });
    return true;
  }
  if (!alreadySelected) select(pageId, [id]);
  if (o.type === 'note') {
    startMove(e, pageEl, S.selection.ids, () => openNotePopover(pageId, id));
    return true;
  }
  // a click (without dragging) on a text box edits it when it was already selected,
  // on a double-click, or right away in the Add Text tool
  const editOnClick = o.type === 'text' && (alreadySelected || e.detail >= 2 || S.tool === 'text');
  const edit = () => editTextObject(pageId, id, { clientX: e.clientX, clientY: e.clientY });
  startMove(e, pageEl, S.selection.ids, editOnClick ? edit : null);
  return true;
}

function startMove(e, pageEl, ids, onClick, onDoubleClick) {
  e.preventDefault();
  const pageId = pageEl.dataset.id;
  const s = scale();
  const start = { x: e.clientX, y: e.clientY };
  const originals = ids.map((id) => objectById(pageId, id)).filter(Boolean);
  const layer = layerOf(pageId, 'objects');
  const els = originals.map((o) => layer.querySelector(`.obj[data-id="${o.id}"]`));
  const box = layer.querySelector('.sel-box');
  const actions = layer.querySelector('.obj-actions');
  let moved = false;
  let dx = 0;
  let dy = 0;
  const move = (ev) => {
    const ddx = ev.clientX - start.x;
    const ddy = ev.clientY - start.y;
    if (!moved && Math.hypot(ddx, ddy) < 3) return;
    moved = true;
    if (actions) actions.style.display = 'none';
    dx = ddx / s;
    dy = ddy / s;
    if (ev.shiftKey) (Math.abs(dx) > Math.abs(dy) ? (dy = 0) : (dx = 0));
    const t = `translate(${dx * s}px, ${dy * s}px)`;
    els.forEach((el) => el && (el.style.transform = t));
    if (box) box.style.transform = t;
  };
  const up = (ev) => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    if (!moved) {
      if (onDoubleClick) onDoubleClick();
      else if (onClick) onClick();
      return;
    }
    const target = pageAtPoint(ev.clientX, ev.clientY);
    if (target && target.dataset.id !== pageId) {
      // dropped on another page: move the objects there
      const srcRect = pageEl.getBoundingClientRect();
      const dstRect = target.getBoundingClientRect();
      const offX = (srcRect.left - dstRect.left) / s;
      const offY = (srcRect.top - dstRect.top) / s;
      const toPage = target.dataset.id;
      const movedObjs = originals.map((o) => translated(o, dx + offX, dy + offY));
      localSet(pageId, objectsOf(pageId).filter((o) => !ids.includes(o.id)));
      localSet(toPage, [...objectsOf(toPage), ...movedObjs]);
      renderPageObjects(pageId);
      renderPageObjects(toPage);
      select(toPage, movedObjs.map((o) => o.id));
      mutate('objects', { ops: movedObjs.map((o) => ({ op: 'move', pageId, toPageId: toPage, object: o, label: 'Move to page' })) });
      return;
    }
    const page = pageById(pageId);
    const movedObjs = originals.map((o) => {
      const b = bounds(o);
      const cdx = clamp(dx, -b.x - b.w + 8, page.w - b.x - 8);
      const cdy = clamp(dy, -b.y - b.h + 8, page.h - b.y - 8);
      return translated(o, cdx, cdy);
    });
    updateObjects(pageId, movedObjs, { label: 'Move' });
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

function startResize(e, pageEl, handle) {
  e.preventDefault();
  e.stopPropagation();
  const pageId = pageEl.dataset.id;
  const o = selectedObjects()[0];
  if (!o) return;
  const s = scale();
  const b0 = elementBounds(pageId, o);
  const layer = layerOf(pageId, 'objects');
  const aspectLocked = o.type === 'image' || o.type === 'symbol';
  let current = o;
  const move = (ev) => {
    const p = pagePoint(pageEl, ev.clientX, ev.clientY);
    if (handle === 'p1' || handle === 'p2') {
      current = { ...o };
      let { x, y } = p;
      const ox = handle === 'p1' ? o.x2 : o.x1;
      const oy = handle === 'p1' ? o.y2 : o.y1;
      if (ev.shiftKey) {
        const ang = Math.round(Math.atan2(y - oy, x - ox) / (Math.PI / 4)) * (Math.PI / 4);
        const len = Math.hypot(x - ox, y - oy);
        x = ox + Math.cos(ang) * len;
        y = oy + Math.sin(ang) * len;
      }
      if (handle === 'p1') { current.x1 = x; current.y1 = y; } else { current.x2 = x; current.y2 = y; }
    } else {
      let { x: nx0, y: ny0 } = b0;
      let nx1 = b0.x + b0.w;
      let ny1 = b0.y + b0.h;
      if (handle.includes('w')) nx0 = Math.min(p.x, nx1 - 4);
      if (handle.includes('e')) nx1 = Math.max(p.x, nx0 + 4);
      if (handle.includes('n')) ny0 = Math.min(p.y, ny1 - 4);
      if (handle.includes('s')) ny1 = Math.max(p.y, ny0 + 4);
      let b1 = { x: nx0, y: ny0, w: nx1 - nx0, h: ny1 - ny0 };
      if ((aspectLocked !== ev.shiftKey) && handle.length === 2 && b0.w && b0.h) {
        const k = Math.max(b1.w / b0.w, b1.h / b0.h);
        const w = b0.w * k;
        const hh = b0.h * k;
        b1 = { x: handle.includes('w') ? nx1 - w : nx0, y: handle.includes('n') ? ny1 - hh : ny0, w, h: hh };
      }
      current = resized(o, b0, b1);
    }
    const oldEl = layer.querySelector(`.obj[data-id="${o.id}"]`);
    const newEl = renderObject(current);
    if (current.type === 'text' && current.text) newEl.classList.remove('placeholder');
    oldEl?.replaceWith(newEl);
    const byId = objectsOf(pageId).map((x) => (x.id === o.id ? current : x));
    localSet(pageId, byId);
    renderSelection();
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    if (current === o) return;
    const final = current.type === 'text' ? withMeasuredLayout(pageId, current) : current;
    updateObjects(pageId, [final], { label: 'Resize' });
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

// ------------------------------------------------------------------ wiring
export function initObjects() {
  on('zoom', () => {
    if (S.editing?.kind === 'object') finishEditing();
    renderAllObjects();
  });
  pagesContainer.addEventListener('dblclick', (e) => {
    const objEl = e.target.closest('.obj-text');
    const pageEl = e.target.closest('.page');
    if (objEl && pageEl && objectsInteractive() && !S.editing) {
      editTextObject(pageEl.dataset.id, objEl.dataset.id, { clientX: e.clientX, clientY: e.clientY });
    }
  });
}

export function hasPendingText() {
  return Boolean(pendingNew);
}

export function selectAllOnPage(pageId) {
  const ids = objectsOf(pageId).map((o) => o.id);
  if (ids.length) select(pageId, ids);
}

export { BOX_TYPES, pageElement };
