// Interactive form fields drawn over PDF widgets.
import { S, on, scale, pageById } from './state.js';
import { h } from './util.js';
import { getJSON, docUrl, mutate } from './api.js';
import { layerOf, visiblePageIds } from './viewer.js';

const cache = new Map();

function fieldsFor(pageId) {
  const page = pageById(pageId);
  const key = `${S.doc.id}:${pageId}:${page.rev}`;
  if (!cache.has(key)) {
    const p = getJSON(docUrl(`/page/${pageId}/forms`)).then((r) => r.fields);
    p.catch(() => cache.delete(key));
    cache.set(key, p);
  }
  return cache.get(key);
}

function commit(pageId, field, value) {
  mutate('forms', { pageId, xref: field.xref, value }).catch(() => {});
}

export async function renderForms(pageId) {
  if (!S.doc?.hasForms) return;
  const layer = layerOf(pageId, 'forms');
  if (!layer || layer.contains(document.activeElement)) return;
  let fields;
  try {
    fields = await fieldsFor(pageId);
  } catch {
    return;
  }
  if (layer.contains(document.activeElement)) return;
  const s = scale();
  const nodes = fields.map((f) => {
    const [x0, y0, x1, y1] = f.rect;
    let el;
    const common = { class: 'form-field', title: f.name, disabled: f.readonly };
    if (f.type === 'checkbox' || f.type === 'radiobutton') {
      el = h('input', { ...common, type: 'checkbox', class: `form-field form-check${f.type === 'radiobutton' ? ' radio' : ''}`, checked: f.value && f.value !== 'Off' });
      el.addEventListener('change', () => commit(pageId, f, el.checked));
    } else if (f.type === 'combobox' || f.type === 'listbox') {
      el = h('select', { ...common, size: f.type === 'listbox' ? Math.max(2, Math.floor((y1 - y0) / 12)) : null },
        f.type === 'combobox' ? h('option', { value: '', text: '' }) : null,
        (f.options || []).map((o) => h('option', { value: o, text: o, selected: o === f.value })));
      el.addEventListener('change', () => commit(pageId, f, el.value));
    } else {
      el = f.multiline ? h('textarea', { ...common }) : h('input', { ...common, type: 'text', maxlength: f.maxlen || null });
      el.value = f.value || '';
      el.addEventListener('change', () => commit(pageId, f, el.value));
      el.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter' && !f.multiline) el.blur();
      });
      const fs = f.fontsize > 0 ? f.fontsize : Math.min(12, (y1 - y0) * 0.65);
      el.style.fontSize = `${fs * s}px`;
    }
    Object.assign(el.style, { left: `${x0 * s}px`, top: `${y0 * s}px`, width: `${(x1 - x0) * s}px`, height: `${(y1 - y0) * s}px` });
    el.addEventListener('pointerdown', (e) => e.stopPropagation());
    return el;
  });
  layer.replaceChildren(...nodes);
}

export function initForms() {
  on('page-visible', (id) => renderForms(id));
  on('page-changed', (id) => renderForms(id));
  on('zoom', () => {
    for (const id of visiblePageIds()) {
      const layer = layerOf(id, 'forms');
      if (layer) layer.replaceChildren();
      renderForms(id);
    }
  });
  document.addEventListener('focusout', (e) => {
    const layer = e.target.closest?.('.layer-forms');
    if (layer) setTimeout(() => {
      if (!layer.contains(document.activeElement)) renderForms(layer.closest('.page').dataset.id);
    }, 400);
  });
}
