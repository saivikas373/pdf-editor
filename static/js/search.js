// Find (with highlighted hits) and find & replace.
import { S, on, scale } from './state.js';
import { $, h, toast, debounce } from './util.js';
import { icon } from './icons.js';
import { postJSON, docUrl, mutate } from './api.js';
import { layerOf, scrollToPage } from './viewer.js';

const bar = $('#search-bar');
let hits = [];
let current = -1;
let lastQuery = '';

function clearHits() {
  document.querySelectorAll('.layer-search').forEach((l) => l.replaceChildren());
}

function renderHits() {
  clearHits();
  const s = scale();
  hits.forEach((hit, i) => {
    const layer = layerOf(hit.pageId, 'search');
    if (!layer) return;
    const [x0, y0, x1, y1] = hit.rect;
    const el = h('div', { class: `search-hit${i === current ? ' current' : ''}` });
    Object.assign(el.style, { left: `${x0 * s - 1}px`, top: `${y0 * s - 1}px`, width: `${(x1 - x0) * s + 2}px`, height: `${(y1 - y0) * s + 2}px` });
    layer.append(el);
  });
}

function go(index) {
  if (!hits.length) return;
  current = (index + hits.length) % hits.length;
  const hit = hits[current];
  const pageIndex = S.doc.pages.findIndex((p) => p.id === hit.pageId);
  scrollToPage(pageIndex, { offsetPt: hit.rect[1], smooth: false });
  renderHits();
  updateCount();
}

let countEl;
function updateCount() {
  if (!countEl) return;
  countEl.textContent = lastQuery ? (hits.length ? `${current + 1} of ${hits.length}` : 'No results') : '';
}

async function runSearch(query, matchCase) {
  lastQuery = query;
  if (!query || !S.doc) {
    hits = [];
    current = -1;
    clearHits();
    updateCount();
    return;
  }
  try {
    const res = await postJSON(docUrl('/search'), { q: query, matchCase });
    if (query !== lastQuery) return;
    hits = res.hits;
    // start from the current page
    const startIdx = hits.findIndex((hit) => S.doc.pages.findIndex((p) => p.id === hit.pageId) >= S.currentPage);
    current = hits.length ? Math.max(0, startIdx) : -1;
    if (hits.length) go(current); else { clearHits(); updateCount(); }
  } catch (err) {
    toast(err.message, { type: 'error' });
  }
}

export function openSearch({ replace = false, query } = {}) {
  if (!S.doc) return;
  bar.hidden = false;
  if (!bar.childElementCount) build();
  const input = bar.querySelector('.q');
  if (query !== undefined) input.value = query;
  const rep = bar.querySelector('.replace-row');
  if (replace) rep.hidden = false;
  input.focus();
  input.select();
  if (input.value && input.value !== lastQuery) triggerSearch();
}

export function closeSearch() {
  bar.hidden = true;
  hits = [];
  lastQuery = '';
  current = -1;
  clearHits();
}

let triggerSearch = () => {};

function build() {
  const q = h('input', { class: 'input q', placeholder: 'Find in document', spellcheck: 'false' });
  const caseCb = h('input', { type: 'checkbox' });
  const wordCb = h('input', { type: 'checkbox' });
  countEl = h('span', { class: 'search-count' });
  const rep = h('input', { class: 'input', placeholder: 'Replace with', spellcheck: 'false' });
  const replaceRow = h('div', { class: 'replace-row', hidden: true, style: { display: 'flex', flexDirection: 'column', gap: '8px' } },
    h('div', { class: 'row' }, rep,
      h('button', { class: 'btn primary', text: 'Replace all', onclick: async () => {
        if (!q.value) return;
        try {
          const res = await mutate('text/replace', { find: q.value, replace: rep.value, matchCase: caseCb.checked, wholeWord: wordCb.checked });
          if (res.replaced) toast(`Replaced ${res.replaced} occurrence${res.replaced === 1 ? '' : 's'}`, { type: 'success' });
          else toast('No matches to replace.');
          if (res.skipped) toast(`${res.skipped} match${res.skipped === 1 ? ' was' : 'es were'} in rotated text and skipped.`);
          lastQuery = '';
          setTimeout(triggerSearch, 300);
        } catch {
          /* toast shown */
        }
      } })),
    h('div', { class: 'hint-box', style: { fontSize: '11.5px' }, text: 'Replacements keep the original font, size and colour. Matches must be within one line.' }));
  triggerSearch = debounce(() => runSearch(q.value, caseCb.checked), 250);
  q.addEventListener('input', triggerSearch);
  q.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      e.preventDefault();
      if (q.value !== lastQuery) triggerSearch.flush();
      else go(current + (e.shiftKey ? -1 : 1));
    } else if (e.key === 'Escape') {
      closeSearch();
    }
  });
  rep.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') closeSearch(); });
  caseCb.addEventListener('change', () => { lastQuery = ''; triggerSearch(); });
  bar.append(
    h('div', { class: 'row' }, q, countEl,
      h('button', { class: 'icon-btn small', html: icon('chevron-up'), 'data-tip': 'Previous (⇧↵)', onclick: () => go(current - 1) }),
      h('button', { class: 'icon-btn small', html: icon('chevron-down'), 'data-tip': 'Next (↵)', onclick: () => go(current + 1) }),
      h('button', { class: 'icon-btn small', html: icon('x'), 'data-tip': 'Close (Esc)', onclick: closeSearch })),
    h('div', { class: 'search-opts' },
      h('label', { class: 'check-row' }, caseCb, h('span', { text: 'Match case' })),
      h('label', { class: 'check-row' }, wordCb, h('span', { text: 'Whole words (replace)' })),
      h('button', { class: 'link-btn', html: `${icon('replace')}<span>Replace…</span>`, style: { marginLeft: 'auto', fontSize: '12px' }, onclick: () => { replaceRow.hidden = !replaceRow.hidden; if (!replaceRow.hidden) rep.focus(); } })),
    replaceRow);
}

/** Search and add redaction boxes over every match. */
export async function redactMatches(query) {
  if (!query?.trim()) {
    toast('Type the text you want to redact.');
    return;
  }
  const res = await postJSON(docUrl('/search'), { q: query.trim(), matchCase: false });
  if (!res.hits.length) {
    toast('No matches found.');
    return;
  }
  const ops = res.hits.map((hit) => {
    const [x0, y0, x1, y1] = hit.rect;
    return { op: 'add', pageId: hit.pageId, object: { id: Math.random().toString(36).slice(2, 11), type: 'redact', x: x0 - 1, y: y0 - 0.5, w: x1 - x0 + 2, h: y1 - y0 + 1, fill: S.prefs.redact.fill }, label: 'Mark redactions' };
  });
  await mutate('objects', { ops });
  toast(`Marked ${ops.length} match${ops.length === 1 ? '' : 'es'} for redaction`, { type: 'success' });
}

export function initSearch() {
  on('zoom', () => { if (hits.length) renderHits(); });
  on('doc', ({ settled }) => { if (settled && hits.length) renderHits(); });
}
