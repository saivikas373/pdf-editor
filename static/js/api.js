// Server communication. All document mutations run through a serial queue so
// operations apply in the order the user performed them.
import { S, emit } from './state.js';
import { toast } from './util.js';

const HEADERS = { 'X-PDF-Editor': '1' };

export class ApiError extends Error {
  constructor(status, message, data = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function parse(res) {
  const type = res.headers.get('Content-Type') || '';
  if (!res.ok) {
    let data = {};
    try {
      data = type.includes('json') ? await res.json() : { error: await res.text() };
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, data.error || `Request failed (${res.status})`, data);
  }
  return type.includes('json') ? res.json() : res.blob();
}

export async function getJSON(url) {
  return parse(await fetch(url, { headers: HEADERS }));
}

export async function postJSON(url, body = {}) {
  return parse(await fetch(url, {
    method: 'POST',
    headers: { ...HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }));
}

export async function postRaw(url, data, headers = {}) {
  return parse(await fetch(url, { method: 'POST', headers: { ...HEADERS, ...headers }, body: data }));
}

/** POST returning a file: resolves {blob, filename}. */
export async function postForFile(url, body = {}) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { ...HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parse(res);
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : 'download';
  return { blob: await res.blob(), filename };
}

export const docUrl = (path = '') => `/api/doc/${S.doc.id}${path}`;

let queue = Promise.resolve();
let pending = 0;

/**
 * Store fresh document info from the server *before* notifying listeners, so every
 * 'doc' listener sees the new state. While more mutations are queued, locally
 * applied (optimistic) object changes are kept.
 */
function applyDoc(doc, { settled, force = false }) {
  if (!S.doc || doc.id !== S.doc.id) return;
  const replaceObjects = settled || force;
  S.doc = { ...doc, objects: replaceObjects ? doc.objects : S.doc.objects };
  emit('doc', { doc, settled, force, replaceObjects });
}
export const pendingCount = () => pending;

/**
 * Queue a document mutation. The server replies with fresh document info,
 * which is broadcast as a 'doc' event.
 */
export function mutate(path, body = {}, { raw = null, headers = {}, quiet = false } = {}) {
  pending += 1;
  emit('busy', pending);
  const docId = S.doc?.id;
  const run = async () => {
    try {
      const url = `/api/doc/${docId}/${path}`;
      const res = raw ? await postRaw(url, raw, headers) : await postJSON(url, body);
      pending -= 1;
      if (res.doc) applyDoc(res.doc, { settled: pending === 0 });
      emit('busy', pending);
      return res.result ?? res;
    } catch (err) {
      pending -= 1;
      emit('busy', pending);
      if (!quiet) toast(err.message, { type: 'error' });
      if (err.data?.closed) emit('closed');
      else resync();
      throw err;
    }
  };
  const p = queue.then(run, run);
  queue = p.catch(() => {});
  return p;
}

export async function resync() {
  if (!S.doc) return;
  try {
    const res = await getJSON(docUrl());
    applyDoc(res.doc, { settled: pending === 0, force: true });
  } catch (err) {
    if (err.data?.closed) emit('closed');
  }
}

export function flushQueue() {
  return queue;
}
