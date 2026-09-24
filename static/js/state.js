// Global application state + a tiny event bus.

export const PX_PER_PT = 96 / 72;

const DEFAULT_PREFS = {
  text: { font: 'Helvetica', size: 14, color: '#111827', bold: false, italic: false, underline: false, strike: false, align: 'left', bg: null, lineHeight: 1.25, opacity: 1 },
  markup: { kind: 'highlight', highlight: '#ffd400', underline: '#e5484d', strike: '#e5484d', squiggly: '#e5484d', opacity: 0.5 },
  draw: { mode: 'pen', pen: { color: '#1d4ed8', width: 2.5, opacity: 1 }, marker: { color: '#ffd400', width: 14, opacity: 0.4 } },
  shape: { kind: 'rect', stroke: '#e5484d', strokeWidth: 2, fill: null, opacity: 1 },
  symbol: { kind: 'check', color: '#15803d', size: 16 },
  whiteout: { fill: '#ffffff' },
  redact: { fill: '#000000' },
  note: { color: '#ffc83d' },
  edittext: { unit: 'line', showBoxes: false },
  sidebar: true,
};

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem('pdfeditor.prefs') || '{}');
    const merged = structuredClone(DEFAULT_PREFS);
    for (const [k, v] of Object.entries(saved)) {
      if (merged[k] && typeof merged[k] === 'object' && v && typeof v === 'object') {
        merged[k] = { ...merged[k], ...v };
        for (const sub of ['pen', 'marker']) if (DEFAULT_PREFS[k]?.[sub]) merged[k][sub] = { ...DEFAULT_PREFS[k][sub], ...(v[sub] || {}) };
      } else if (k in merged) {
        merged[k] = v;
      }
    }
    return merged;
  } catch {
    return structuredClone(DEFAULT_PREFS);
  }
}

export const S = {
  config: { fonts: [], ocr: { available: false, languages: [] } },
  doc: null, // document info from the server
  zoom: 1,
  fit: 'width', // 'width' | 'page' | null
  tool: 'select',
  view: 'edit', // 'edit' | 'pages'
  selection: null, // { pageId, ids: [...] }
  currentPage: 0,
  prefs: loadPrefs(),
  placing: null, // pending image/signature placement
  editing: null, // active inline text editor
};

export function savePrefs() {
  try {
    localStorage.setItem('pdfeditor.prefs', JSON.stringify(S.prefs));
  } catch {
    /* storage may be unavailable */
  }
}

export function scale() {
  return S.zoom * PX_PER_PT;
}

const listeners = new Map();
export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}
export function emit(event, data) {
  for (const fn of listeners.get(event) || []) {
    try {
      fn(data);
    } catch (err) {
      console.error(`listener for ${event} failed`, err);
    }
  }
}

export function pageById(id) {
  return S.doc?.pages.find((p) => p.id === id) || null;
}

export function pageIndex(id) {
  return S.doc ? S.doc.pages.findIndex((p) => p.id === id) : -1;
}

export function objectsOf(pageId) {
  return (S.doc?.objects && S.doc.objects[pageId]) || [];
}
