// Page thumbnails (sidebar) and the page organizer view.
import { S, emit, on } from './state.js';
import { $, h, clamp, pickFiles, showMenu, toast, isMac, downloadBlob } from './util.js';
import { icon } from './icons.js';
import { mutate, postForFile, docUrl } from './api.js';
import { scrollToPage } from './viewer.js';

const thumbsEl = $('#thumbs');
const organizer = $('#organizer');
const grid = $('#organizer-grid');
const bar = $('#organizer-bar');
const thumbEls = new Map();
let thumbObserver;
const orgSelected = new Set();
let lastClicked = null;

function thumbUrl(page, cssWidth) {
  const dpr = window.devicePixelRatio || 1;
  const s = clamp(Math.ceil((cssWidth / page.w) * dpr * 20) / 20, 0.05, 2);
  return `/api/doc/${S.doc.id}/page/${page.id}.png?rev=${page.rev}&scale=${s}`;
}

// ------------------------------------------------------------------ sidebar
export function renderThumbs() {
  if (!S.doc) {
    thumbsEl.replaceChildren();
    thumbEls.clear();
    return;
  }
  const width = 128;
  const nodes = S.doc.pages.map((page, i) => {
    let el = thumbEls.get(page.id);
    if (!el) {
      el = h('div', { class: 'thumb', dataset: { id: page.id } },
        h('div', { class: 'thumb-frame' }, h('img', { alt: '', draggable: 'false' })),
        h('div', { class: 'thumb-num' }));
      thumbEls.set(page.id, el);
      thumbObserver.observe(el);
    }
    const frame = el.querySelector('.thumb-frame');
    const ratio = page.h / page.w;
    const w = ratio > 1.4 ? width / (ratio / 1.4) : width;
    frame.style.width = `${w}px`;
    frame.style.height = `${w * ratio}px`;
    el.querySelector('.thumb-num').textContent = String(i + 1);
    el.dataset.index = i;
    el._url = thumbUrl(page, w);
    if (el._visible) {
      const img = el.querySelector('img');
      if (img.getAttribute('src') !== el._url) img.src = el._url;
    }
    el.classList.toggle('active', i === S.currentPage);
    return el;
  });
  const ids = new Set(S.doc.pages.map((p) => p.id));
  for (const [id, el] of thumbEls) {
    if (!ids.has(id)) {
      thumbObserver.unobserve(el);
      thumbEls.delete(id);
    }
  }
  thumbsEl.replaceChildren(...nodes);
}

function markActiveThumb() {
  for (const el of thumbEls.values()) el.classList.toggle('active', Number(el.dataset.index) === S.currentPage);
  const active = [...thumbEls.values()].find((el) => Number(el.dataset.index) === S.currentPage);
  if (active) {
    const r = active.getBoundingClientRect();
    const c = thumbsEl.getBoundingClientRect();
    if (r.top < c.top || r.bottom > c.bottom) active.scrollIntoView({ block: 'nearest' });
  }
}

function pageMenu(pageId, x, y) {
  const index = S.doc.pages.findIndex((p) => p.id === pageId);
  showMenu(null, [
    { header: `Page ${index + 1}` },
    { label: 'Rotate clockwise', icon: 'rotate-cw', onClick: () => rotatePages([pageId], 90) },
    { label: 'Rotate counter-clockwise', icon: 'rotate-ccw', onClick: () => rotatePages([pageId], 270) },
    { label: 'Duplicate', icon: 'copy', onClick: () => mutate('pages/duplicate', { pageIds: [pageId] }) },
    { label: 'Insert blank page after', icon: 'file-plus', onClick: () => mutate('pages/blank', { at: index + 1 }) },
    { label: 'Insert PDF after…', icon: 'files', onClick: () => insertPdf(index + 1) },
    { label: 'Extract this page', icon: 'extract', onClick: () => extractPages([pageId]) },
    '-',
    { label: 'Delete page', icon: 'trash', disabled: S.doc.pages.length < 2, onClick: () => deletePages([pageId]) },
  ], { x, y });
}

function initSidebarDrag() {
  thumbsEl.addEventListener('contextmenu', (e) => {
    const t = e.target.closest('.thumb');
    if (!t) return;
    e.preventDefault();
    pageMenu(t.dataset.id, e.clientX, e.clientY);
  });
  thumbsEl.addEventListener('pointerdown', (e) => {
    const t = e.target.closest('.thumb');
    if (!t || e.button !== 0) return;
    e.preventDefault();
    const start = { x: e.clientX, y: e.clientY };
    let dragging = false;
    let line = null;
    let dropIndex = null;
    const move = (ev) => {
      if (!dragging && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 6) return;
      dragging = true;
      t.classList.add('dragging');
      const thumbs = [...thumbsEl.querySelectorAll('.thumb')];
      dropIndex = thumbs.length;
      for (let i = 0; i < thumbs.length; i++) {
        const r = thumbs[i].getBoundingClientRect();
        if (ev.clientY < r.top + r.height / 2) { dropIndex = i; break; }
      }
      line?.remove();
      line = h('div', { class: 'thumb-drop-line' });
      if (dropIndex < thumbs.length) thumbs[dropIndex].before(line); else thumbsEl.append(line);
      const c = thumbsEl.getBoundingClientRect();
      if (ev.clientY < c.top + 30) thumbsEl.scrollTop -= 12;
      if (ev.clientY > c.bottom - 30) thumbsEl.scrollTop += 12;
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      line?.remove();
      t.classList.remove('dragging');
      if (!dragging) {
        scrollToPage(Number(t.dataset.index));
        return;
      }
      const order = S.doc.pages.map((p) => p.id);
      const from = order.indexOf(t.dataset.id);
      order.splice(from, 1);
      const to = dropIndex > from ? dropIndex - 1 : dropIndex;
      order.splice(to, 0, t.dataset.id);
      if (order.join() !== S.doc.pages.map((p) => p.id).join()) mutate('pages/reorder', { order });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });
}

// ------------------------------------------------------------------ page operations
export function rotatePages(pageIds, angle) {
  const hasObjects = pageIds.some((id) => S.doc.objects?.[id]?.length);
  if (hasObjects) toast('Items you added on rotated pages were made permanent. Undo to get them back as editable.');
  return mutate('pages/rotate', { pageIds, angle });
}

export function deletePages(pageIds) {
  if (pageIds.length >= S.doc.pages.length) {
    toast('A document needs at least one page.', { type: 'error' });
    return null;
  }
  return mutate('pages/delete', { pageIds }).then(() => {
    toast(`Deleted ${pageIds.length} page${pageIds.length > 1 ? 's' : ''}`, { action: () => emit('undo'), actionLabel: 'Undo' });
  });
}

export async function extractPages(pageIds) {
  try {
    const { blob, filename } = await postForFile(docUrl('/extract'), { pageIds });
    downloadBlob(blob, filename);
  } catch (err) {
    toast(err.message, { type: 'error' });
  }
}

export async function insertPdf(at, file) {
  let f = file;
  if (!f) [f] = await pickFiles({ accept: 'application/pdf,.pdf' });
  if (!f) return;
  let password = null;
  for (;;) {
    try {
      const headers = { 'X-Filename': encodeURIComponent(f.name), 'X-At': String(at) };
      if (password) headers['X-Password'] = encodeURIComponent(password);
      const res = await mutate('pages/insert-pdf', null, { raw: f, headers, quiet: true });
      toast(`Inserted ${res.inserted} page${res.inserted === 1 ? '' : 's'}`, { type: 'success' });
      return;
    } catch (err) {
      if (err.status === 401) {
        const { passwordPrompt } = await import('./dialogs.js');
        password = await passwordPrompt(f.name, err.data?.wrong);
        if (password) continue;
        return;
      }
      toast(err.message, { type: 'error' });
      return;
    }
  }
}

export async function insertImages(at, files, pageSize = 'fit') {
  let list = files;
  if (!list) list = await pickFiles({ accept: 'image/*', multiple: true });
  if (!list?.length) return;
  const { uploadImage } = await import('./tools.js');
  try {
    const assets = [];
    for (const f of list) assets.push((await uploadImage(f)).asset);
    const res = await mutate('pages/insert-images', { assets, at, pageSize });
    toast(`Added ${res.inserted} page${res.inserted === 1 ? '' : 's'}`, { type: 'success' });
  } catch (err) {
    toast(err.message, { type: 'error' });
  }
}

// ------------------------------------------------------------------ organizer
export function openOrganizer() {
  if (!S.doc) return;
  emit('before-organizer');
  S.view = 'pages';
  document.body.classList.add('view-pages');
  organizer.hidden = false;
  orgSelected.clear();
  const current = S.doc.pages[S.currentPage];
  if (current) orgSelected.add(current.id);
  renderOrganizer();
}

export function closeOrganizer() {
  S.view = 'edit';
  document.body.classList.remove('view-pages');
  organizer.hidden = true;
  emit('organizer-closed');
}

function selectedIds() {
  return S.doc.pages.map((p) => p.id).filter((id) => orgSelected.has(id));
}

function renderBar() {
  const ids = selectedIds();
  const n = ids.length;
  const btn = (iconName, label, fn, disabled = false, tip = null) => h('button', { class: 'btn ghost', disabled, 'data-tip': tip, html: `${icon(iconName)}<span>${label}</span>`, onclick: fn });
  const lastIndex = n ? Math.max(...ids.map((id) => S.doc.pages.findIndex((p) => p.id === id))) + 1 : S.doc.pages.length;
  bar.replaceChildren(
    h('span', { class: 'count', text: n ? `${n} of ${S.doc.pages.length} selected` : `${S.doc.pages.length} pages` }),
    h('button', { class: 'btn ghost', text: n === S.doc.pages.length ? 'Select none' : 'Select all', onclick: () => {
      if (n === S.doc.pages.length) orgSelected.clear(); else S.doc.pages.forEach((p) => orgSelected.add(p.id));
      renderOrganizer();
    } }),
    h('div', { class: 'sep' }),
    btn('rotate-ccw', 'Rotate left', () => rotatePages(ids, 270), !n),
    btn('rotate-cw', 'Rotate right', () => rotatePages(ids, 90), !n),
    btn('copy', 'Duplicate', () => mutate('pages/duplicate', { pageIds: ids }), !n),
    btn('extract', 'Extract', () => extractPages(ids), !n, 'Download the selected pages as a new PDF'),
    btn('trash', 'Delete', () => { deletePages(ids); orgSelected.clear(); }, !n || n >= S.doc.pages.length),
    h('div', { class: 'sep' }),
    btn('file-plus', 'Blank page', () => mutate('pages/blank', { at: lastIndex })),
    btn('files', 'Insert PDF', () => insertPdf(lastIndex)),
    btn('image-plus', 'Insert images', () => insertImages(lastIndex)),
    h('div', { class: 'grow' }),
    h('button', { class: 'btn primary', text: 'Done', onclick: closeOrganizer }),
  );
}

export function renderOrganizer() {
  if (S.view !== 'pages' || !S.doc) return;
  for (const id of [...orgSelected]) if (!S.doc.pages.some((p) => p.id === id)) orgSelected.delete(id);
  renderBar();
  const cards = S.doc.pages.map((page, i) => {
    const ratio = page.h / page.w;
    const fw = ratio >= 180 / 140 ? 180 / ratio : 140;
    const img = h('img', { src: thumbUrl(page, fw), alt: `Page ${i + 1}`, draggable: 'false', loading: 'lazy' });
    img.style.width = `${fw}px`;
    img.style.height = `${fw * ratio}px`;
    const act = (iconName, tip, fn) => h('button', { class: 'icon-btn', 'data-tip': tip, html: icon(iconName), onpointerdown: (e) => e.stopPropagation(), onclick: (e) => { e.stopPropagation(); fn(); } });
    return h('div', { class: `org-card${orgSelected.has(page.id) ? ' selected' : ''}`, dataset: { id: page.id, index: i } },
      h('div', { class: 'frame' }, img),
      h('div', { class: 'num', text: String(i + 1) }),
      h('div', { class: 'card-actions' },
        act('rotate-ccw', 'Rotate left', () => rotatePages([page.id], 270)),
        act('rotate-cw', 'Rotate right', () => rotatePages([page.id], 90)),
        act('copy', 'Duplicate', () => mutate('pages/duplicate', { pageIds: [page.id] })),
        act('trash', 'Delete', () => deletePages([page.id]))));
  });
  grid.replaceChildren(...cards);
}

function initOrganizerInteractions() {
  grid.addEventListener('dblclick', (e) => {
    const card = e.target.closest('.org-card');
    if (!card) return;
    closeOrganizer();
    requestAnimationFrame(() => scrollToPage(Number(card.dataset.index)));
  });
  grid.addEventListener('contextmenu', (e) => {
    const card = e.target.closest('.org-card');
    if (!card) return;
    e.preventDefault();
    pageMenu(card.dataset.id, e.clientX, e.clientY);
  });
  grid.addEventListener('pointerdown', (e) => {
    const card = e.target.closest('.org-card');
    if (e.button !== 0) return;
    if (!card) {
      if (e.target === grid) { orgSelected.clear(); renderOrganizer(); }
      return;
    }
    e.preventDefault();
    const id = card.dataset.id;
    const multi = isMac ? e.metaKey : e.ctrlKey;
    const start = { x: e.clientX, y: e.clientY };
    let dragging = false;
    let ghost = null;
    let indicator = null;
    let dropIndex = null;
    const move = (ev) => {
      if (!dragging && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 6) return;
      if (!dragging) {
        dragging = true;
        if (!orgSelected.has(id)) {
          orgSelected.clear();
          orgSelected.add(id);
          renderOrganizer();
        }
        const count = orgSelected.size;
        ghost = h('div', { class: 'drag-ghost' }, card.querySelector('img').cloneNode());
        if (count > 1) ghost.append(h('div', { text: String(count), style: { position: 'absolute', top: '-8px', right: '-8px', background: 'var(--accent)', color: '#fff', borderRadius: '10px', padding: '1px 7px', fontWeight: '700', fontSize: '12px' } }));
        document.body.append(ghost);
        grid.querySelectorAll('.org-card').forEach((c) => { if (orgSelected.has(c.dataset.id)) c.classList.add('dragging'); });
      }
      ghost.style.left = `${ev.clientX + 8}px`;
      ghost.style.top = `${ev.clientY + 8}px`;
      const cards = [...grid.querySelectorAll('.org-card')];
      let best = null;
      for (const c of cards) {
        const r = c.getBoundingClientRect();
        if (ev.clientY >= r.top - 12 && ev.clientY <= r.bottom + 12) {
          const d = Math.abs(ev.clientX - (r.left + r.width / 2));
          if (!best || d < best.d) best = { c, r, d };
        }
      }
      indicator?.remove();
      if (best) {
        const after = ev.clientX > best.r.left + best.r.width / 2;
        dropIndex = Number(best.c.dataset.index) + (after ? 1 : 0);
        indicator = h('div', { class: 'org-drop' });
        indicator.style.left = after ? `${best.c.offsetWidth + 7}px` : '-13px';
        best.c.append(indicator);
      }
      const gr = grid.getBoundingClientRect();
      if (ev.clientY < gr.top + 40) grid.scrollTop -= 14;
      if (ev.clientY > gr.bottom - 40) grid.scrollTop += 14;
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      ghost?.remove();
      indicator?.remove();
      grid.querySelectorAll('.dragging').forEach((c) => c.classList.remove('dragging'));
      if (!dragging) {
        const index = Number(card.dataset.index);
        if (e.shiftKey && lastClicked !== null) {
          const [a, b] = [Math.min(lastClicked, index), Math.max(lastClicked, index)];
          for (let i = a; i <= b; i++) orgSelected.add(S.doc.pages[i].id);
        } else if (multi) {
          orgSelected.has(id) ? orgSelected.delete(id) : orgSelected.add(id);
          lastClicked = index;
        } else {
          orgSelected.clear();
          orgSelected.add(id);
          lastClicked = index;
        }
        renderOrganizer();
        return;
      }
      if (dropIndex === null) return;
      const ids = S.doc.pages.map((p) => p.id);
      const moving = ids.filter((x) => orgSelected.has(x));
      const before = ids.slice(0, dropIndex).filter((x) => !orgSelected.has(x));
      const after = ids.slice(dropIndex).filter((x) => !orgSelected.has(x));
      const order = [...before, ...moving, ...after];
      if (order.join() !== ids.join()) mutate('pages/reorder', { order });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });
}

export function initPages() {
  thumbObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const el = entry.target;
      el._visible = entry.isIntersecting;
      if (entry.isIntersecting) {
        const img = el.querySelector('img');
        if (el._url && img.getAttribute('src') !== el._url) img.src = el._url;
      }
    }
  }, { root: thumbsEl, rootMargin: '400px 0px' });
  initSidebarDrag();
  initOrganizerInteractions();
  $('#btn-organize').addEventListener('click', openOrganizer);
  on('current-page', markActiveThumb);
  on('doc', () => {
    renderThumbs();
    renderOrganizer();
  });
  on('open-organizer', openOrganizer);
}
