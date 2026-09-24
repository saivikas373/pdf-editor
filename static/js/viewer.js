// Page viewer: page elements, zoom, lazy rendering, coordinates.
import { S, emit, on, scale, PX_PER_PT, pageById } from './state.js';
import { $, h, clamp, rafThrottle, debounce } from './util.js';

const viewer = $('#viewer');
const pagesEl = $('#pages');
const pageEls = new Map();
const visible = new Set();
let observer;

export const viewerEl = viewer;
export const pagesContainer = pagesEl;

export function initViewer() {
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const el = entry.target;
      const id = el.dataset.id;
      if (entry.isIntersecting) {
        if (!visible.has(id)) {
          visible.add(id);
          refreshImage(el);
          emit('page-visible', id);
        }
      } else if (visible.has(id)) {
        visible.delete(id);
        unloadImage(el);
      }
    }
  }, { root: viewer, rootMargin: '1400px 0px' });

  viewer.addEventListener('scroll', rafThrottle(updateCurrentPage), { passive: true });
  viewer.addEventListener('wheel', (e) => {
    if (!S.doc || !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0035));
    setZoom(S.zoom * factor, { anchor: e });
  }, { passive: false });
  window.addEventListener('resize', debounce(() => {
    if (S.doc && S.fit) applyFit();
  }, 120));
}

export function pageElement(id) {
  return pageEls.get(id) || null;
}

export function layerOf(pageId, name) {
  return pageEls.get(pageId)?.querySelector(`.layer-${name}`) || null;
}

export function visiblePageIds() {
  return [...visible];
}

export function isVisible(id) {
  return visible.has(id);
}

function imageUrl(page) {
  const dpr = window.devicePixelRatio || 1;
  const bucket = clamp(Math.ceil(scale() * dpr * 4) / 4, 0.25, 6);
  return `/api/doc/${S.doc.id}/page/${page.id}.png?rev=${page.rev}&scale=${bucket}`;
}

function refreshImage(el, force = false) {
  const page = pageById(el.dataset.id);
  if (!page || !S.doc) return;
  const url = imageUrl(page);
  if (el._wantUrl === url && !force) return;
  el._wantUrl = url;
  const img = el.querySelector('.page-img');
  if (!img.getAttribute('src')) el.classList.add('loading');
  const loader = new Image();
  loader.onload = () => {
    if (el._wantUrl !== url) return;
    img.src = url;
    el.classList.remove('loading');
    emit('page-image', el.dataset.id);
  };
  loader.onerror = () => {
    if (el._wantUrl === url) el.classList.remove('loading');
  };
  loader.src = url;
}

function unloadImage(el) {
  el._wantUrl = null;
  const img = el.querySelector('.page-img');
  img.removeAttribute('src');
}

/** Resolves once the current image of a page has loaded (used to swap editors without flicker). */
export function whenPageImageLoaded(pageId, { rev = null, timeout = 8000 } = {}) {
  // Waiting for "an image to load" is not enough after an edit: the new one may not
  // have been asked for yet, in which case the old picture is already loaded and this
  // returns at once - showing the reader their old text back for a moment.  Given a
  // revision, it waits for that revision to be the picture on screen.
  return new Promise((resolve) => {
    const el = pageEls.get(pageId);
    if (!el) return resolve();
    const showing = () => {
      const src = el.querySelector('.page-img').getAttribute('src') || '';
      if (!el._wantUrl || src !== el._wantUrl) return false;
      return rev == null || src.includes(`rev=${rev}`);
    };
    if (showing()) return resolve();
    const done = () => {
      clearTimeout(timer);
      off();
      resolve();
    };
    const handler = (e) => { if (e.detail === pageId && showing()) done(); };
    const off = () => document.removeEventListener('page-image-loaded', handler);
    document.addEventListener('page-image-loaded', handler);
    const timer = setTimeout(done, timeout);
  });
}

function createPageEl(page) {
  return h('div', { class: 'page loading', dataset: { id: page.id } },
    h('img', { class: 'page-img', draggable: 'false', alt: '' }),
    h('div', { class: 'layer layer-search' }),
    h('div', { class: 'layer layer-text' }),
    h('div', { class: 'layer layer-forms' }),
    h('div', { class: 'layer layer-objects' }),
    h('div', { class: 'layer layer-draft' }),
    h('div', { class: 'page-label' }));
}

function sizePage(el, page) {
  const s = scale();
  el.style.width = `${Math.round(page.w * s)}px`;
  el.style.height = `${Math.round(page.h * s)}px`;
}

/** Bring page elements in line with S.doc.pages (add / remove / reorder / re-render). */
export function syncPages() {
  if (!S.doc) {
    for (const el of pageEls.values()) el.remove();
    pageEls.clear();
    visible.clear();
    return;
  }
  const ids = new Set(S.doc.pages.map((p) => p.id));
  for (const [id, el] of pageEls) {
    if (!ids.has(id)) {
      observer.unobserve(el);
      el.remove();
      pageEls.delete(id);
      visible.delete(id);
    }
  }
  let prev = null;
  const changed = [];
  S.doc.pages.forEach((page, i) => {
    let el = pageEls.get(page.id);
    if (!el) {
      el = createPageEl(page);
      pageEls.set(page.id, el);
      el.dataset.rev = page.rev;
      (prev ? prev.after(el) : pagesEl.prepend(el));
      observer.observe(el);
    } else {
      const expected = prev ? prev.nextElementSibling : pagesEl.firstElementChild;
      if (expected !== el) (prev ? prev.after(el) : pagesEl.prepend(el));
      if (el.dataset.rev !== String(page.rev)) {
        el.dataset.rev = page.rev;
        changed.push(page.id);
        if (visible.has(page.id)) refreshImage(el, true);
      }
    }
    sizePage(el, page);
    el.querySelector('.page-label').textContent = `${i + 1}`;
    prev = el;
  });
  for (const id of changed) emit('page-changed', id);
  updateCurrentPage();
}

export function setZoom(z, { anchor = null, keepFit = false } = {}) {
  if (!S.doc) return;
  z = clamp(z, 0.1, 6);
  const oldScale = scale();
  const rect = viewer.getBoundingClientRect();
  const ax = anchor ? anchor.clientX - rect.left : viewer.clientWidth / 2;
  const ay = anchor ? anchor.clientY - rect.top : viewer.clientHeight / 2;
  const contentX = viewer.scrollLeft + ax;
  const contentY = viewer.scrollTop + ay;
  S.zoom = z;
  if (!keepFit) S.fit = null;
  for (const page of S.doc.pages) {
    const el = pageEls.get(page.id);
    if (el) sizePage(el, page);
  }
  const ratio = scale() / oldScale;
  viewer.scrollLeft = contentX * ratio - ax;
  viewer.scrollTop = contentY * ratio - ay;
  emit('zoom', S.zoom);
  reloadImagesSoon();
}

const reloadImagesSoon = debounce(() => {
  for (const id of visible) {
    const el = pageEls.get(id);
    if (el) refreshImage(el);
  }
}, 180);

export function applyFit() {
  if (!S.doc || !S.doc.pages.length) return;
  if (viewer.clientWidth < 120) return; // hidden (e.g. page organizer open): fit again when shown
  const availW = viewer.clientWidth - 64;
  const availH = viewer.clientHeight - 48;
  const maxW = Math.max(...S.doc.pages.map((p) => p.w));
  const first = S.doc.pages[S.currentPage] || S.doc.pages[0];
  let z;
  if (S.fit === 'page') z = Math.min(availW / (first.w * PX_PER_PT), availH / (first.h * PX_PER_PT));
  else z = Math.min(availW / (maxW * PX_PER_PT), 2.5);
  const fit = S.fit;
  setZoom(z, { keepFit: true });
  S.fit = fit;
}

export function fit(mode) {
  S.fit = mode;
  applyFit();
}

function updateCurrentPage() {
  if (!S.doc) return;
  const mid = viewer.getBoundingClientRect().top + viewer.clientHeight * 0.35;
  let best = 0;
  let bestDist = Infinity;
  S.doc.pages.forEach((page, i) => {
    const el = pageEls.get(page.id);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const dist = mid < r.top ? r.top - mid : mid > r.bottom ? mid - r.bottom : 0;
    if (dist < bestDist) {
      bestDist = dist;
      best = i;
    }
  });
  if (best !== S.currentPage) {
    S.currentPage = best;
    emit('current-page', best);
  }
}

export function scrollToPage(index, { offsetPt = null, smooth = false } = {}) {
  const page = S.doc?.pages[index];
  if (!page) return;
  const el = pageEls.get(page.id);
  if (!el) return;
  let top = el.offsetTop - 16;
  if (offsetPt !== null) top = el.offsetTop + offsetPt * scale() - viewer.clientHeight * 0.3;
  viewer.scrollTo({ top: Math.max(0, top), behavior: smooth ? 'smooth' : 'auto' });
  S.currentPage = index;
  emit('current-page', index);
}

export function pagePoint(el, clientX, clientY) {
  const r = el.getBoundingClientRect();
  const s = scale();
  return { x: (clientX - r.left) / s, y: (clientY - r.top) / s };
}

export function pageAtPoint(clientX, clientY) {
  for (const el of pageEls.values()) {
    const r = el.getBoundingClientRect();
    if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) return el;
  }
  return null;
}

/** Sample the rendered page colour around a rectangle (in pt) - used to blend editors in. */
const sampleCanvas = document.createElement('canvas');
export function sampleBackground(pageId, rect) {
  const el = pageEls.get(pageId);
  const img = el?.querySelector('.page-img');
  if (!img || !img.complete || !img.naturalWidth) return '#ffffff';
  const page = pageById(pageId);
  const k = img.naturalWidth / page.w;
  const pts = [
    [rect[0] - 2, rect[1] - 1], [rect[2] + 2, rect[1] - 1], [rect[0] - 2, rect[3] + 1], [rect[2] + 2, rect[3] + 1],
    [(rect[0] + rect[2]) / 2, rect[1] - 1.5], [(rect[0] + rect[2]) / 2, rect[3] + 1.5],
  ];
  sampleCanvas.width = 1;
  sampleCanvas.height = 1;
  const ctx = sampleCanvas.getContext('2d', { willReadFrequently: true });
  const counts = new Map();
  for (const [x, y] of pts) {
    const px = clamp(Math.round(x * k), 0, img.naturalWidth - 1);
    const py = clamp(Math.round(y * k), 0, img.naturalHeight - 1);
    try {
      ctx.clearRect(0, 0, 1, 1);
      ctx.drawImage(img, px, py, 1, 1, 0, 0, 1, 1);
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
      const key = `${r >> 3},${g >> 3},${b >> 3}`;
      const cur = counts.get(key) || { n: 0, r, g, b };
      cur.n += 1;
      counts.set(key, cur);
    } catch {
      return '#ffffff';
    }
  }
  const best = [...counts.values()].sort((a, b) => b.n - a.n)[0];
  return best ? `rgb(${best.r}, ${best.g}, ${best.b})` : '#ffffff';
}

// forward image load events as DOM events for whenPageImageLoaded
on('page-image', (id) => document.dispatchEvent(new CustomEvent('page-image-loaded', { detail: id })));
