// Application bootstrap and wiring.
import { S, on } from './state.js';
import { $, h, toast, setLoading, pickFiles, showMenu, closeMenu, initTooltips, isMac, mod } from './util.js';
import { hydrateIcons, icon } from './icons.js';
import { getJSON, postJSON, postRaw, mutate, flushQueue, pendingCount } from './api.js';
import * as viewer from './viewer.js';
import * as objects from './objects.js';
import * as textedit from './textedit.js';
import * as tools from './tools.js';
import * as panel from './panel.js';
import * as pages from './pages.js';
import * as dialogs from './dialogs.js';
import * as search from './search.js';
import { initForms } from './forms.js';
import { openSignatureDialog } from './signature.js';

const K = isMac ? '⌘' : 'Ctrl+';

// ------------------------------------------------------------------ options drawer
/** On a phone the options panel is a drawer; on a wide screen it is always there. */
const narrow = () => window.matchMedia('(max-width: 760px)').matches;
const setPanel = (open) => document.body.classList.toggle('panel-open', open && narrow());

// tools whose panel is worth interrupting the page for: 'select' only carries
// mouse-and-keyboard tips, and image/sign open a dialog of their own
const NO_OPTIONS = new Set(['select', 'image', 'signature']);

/** Picking a tool on a phone should show what that tool can do, not hide it behind a tab. */
function showToolOptions(id) {
  if (!narrow() || NO_OPTIONS.has(id) || !S.doc || S.view === 'pages') return;
  setPanel(true);
}

// ------------------------------------------------------------------ toolbar
function buildToolbar() {
  const bar = $('#toolbar');
  const items = [];
  for (const t of tools.TOOLS) {
    if (t === '|') {
      items.push(h('div', { class: 'tool-sep' }));
      continue;
    }
    const b = h('button', { class: `tool-btn${t.id === S.tool ? ' active' : ''}`, dataset: { tool: t.id }, 'data-tip': t.tip },
      h('span', { class: 'tool-icon', html: icon(t.icon) }), h('span', { class: 'tool-label', text: t.label }));
    b.addEventListener('click', () => { tools.setTool(t.id); showToolOptions(t.id); });
    items.push(b);
  }
  items.push(h('div', { class: 'grow' }));
  const pagesBtn = h('button', { class: 'tool-btn', 'data-tip': 'Organize, rotate, insert and delete pages' },
    h('span', { class: 'tool-icon', html: icon('pages') }), h('span', { class: 'tool-label', text: 'Pages' }));
  pagesBtn.addEventListener('click', () => (S.view === 'pages' ? pages.closeOrganizer() : pages.openOrganizer()));
  const toolsBtn = h('button', { class: 'tool-btn', 'data-tip': 'More tools' },
    h('span', { class: 'tool-icon', html: icon('tools') }), h('span', { class: 'tool-label', text: 'Tools' }));
  toolsBtn.addEventListener('click', () => showMenu(toolsBtn, toolsMenuItems(), { align: 'right' }));
  items.push(pagesBtn, toolsBtn);
  bar.replaceChildren(...items);
}

function toolsMenuItems() {
  const at = S.currentPage + 1;
  return [
    { header: 'Pages' },
    { label: 'Organize pages', icon: 'grid', onClick: pages.openOrganizer },
    { label: 'Insert blank page', icon: 'file-plus', onClick: () => mutate('pages/blank', { at }) },
    { label: 'Insert pages from PDF…', icon: 'files', onClick: () => pages.insertPdf(at) },
    { label: 'Insert images as pages…', icon: 'image-plus', onClick: () => pages.insertImages(at) },
    { label: 'Crop pages…', icon: 'crop', onClick: dialogs.cropDialog },
    '-',
    { header: 'Document' },
    { label: 'Find & replace…', icon: 'replace', shortcut: `${K}F`, onClick: () => search.openSearch({ replace: true }) },
    { label: 'Add watermark…', icon: 'droplet', onClick: dialogs.watermarkDialog },
    { label: 'Page numbers & headers…', icon: 'hash', onClick: dialogs.pageNumbersDialog },
    { label: S.config.ocr.available ? 'Recognize text (OCR)…' : 'Recognize text (OCR) — install Tesseract', icon: 'ocr', disabled: !S.config.ocr.available, onClick: dialogs.ocrDialog },
    { label: 'Document properties…', icon: 'info', onClick: dialogs.metadataDialog },
    '-',
    { header: 'Export' },
    { label: 'Compress & download…', icon: 'compress', onClick: () => dialogs.downloadDialog() },
    { label: 'Protect with password…', icon: 'lock', onClick: () => dialogs.downloadDialog({ focus: 'password' }) },
    { label: 'Split PDF…', icon: 'scissors', onClick: dialogs.splitDialog },
    { label: 'Extract pages…', icon: 'extract', onClick: dialogs.extractDialog },
    { label: 'Export pages as images…', icon: 'image-down', onClick: dialogs.exportImagesDialog },
    { label: 'Extract text (.txt)', icon: 'file-text', onClick: dialogs.extractText },
    { label: 'Extract embedded images', icon: 'image', onClick: dialogs.extractImages },
  ];
}

// ------------------------------------------------------------------ document lifecycle
function finishAllEditing() {
  if (S.editing?.kind === 'object') objects.finishEditing();
  if (S.editing?.kind === 'text') textedit.commitEditor();
  objects.closeNotePopover();
}

function loadDocument(doc) {
  finishAllEditing();
  search.closeSearch();
  if (S.view === 'pages') pages.closeOrganizer();
  S.doc = doc;
  S.selection = null;
  S.currentPage = 0;
  rememberDoc(doc.id);
  textedit.invalidateText();
  document.body.classList.remove('no-doc');
  $('.doc-title').hidden = false;
  $('#doc-filename').value = doc.filename;
  document.title = `${doc.filename} — PDF Editor`;
  viewer.syncPages();
  requestAnimationFrame(() => {
    viewer.fit('width');
    viewer.viewerEl.scrollTop = 0;
    objects.renderAllObjects();
  });
  pages.renderThumbs();
  panel.render();
  updateHeader();
  tools.setTool(S.tool === 'signature' || S.tool === 'image' ? 'select' : S.tool);
}

function closeDocument() {
  finishAllEditing();
  rememberDoc(null);
  S.doc = null;
  S.selection = null;
  viewer.syncPages();
  pages.renderThumbs();
  panel.render();
  document.body.classList.add('no-doc');
  $('.doc-title').hidden = true;
  document.title = 'PDF Editor';
}

/** Remember the open document per tab so a page reload brings it straight back. */
function rememberDoc(id) {
  try {
    if (id) sessionStorage.setItem('pdfeditor.docId', id);
    else sessionStorage.removeItem('pdfeditor.docId');
  } catch {
    /* storage unavailable */
  }
}

function rememberedDoc() {
  try {
    return sessionStorage.getItem('pdfeditor.docId');
  } catch {
    return null;
  }
}

async function confirmDiscard() {
  if (!S.doc?.dirty) return true;
  return dialogs.confirmDialog({
    title: 'Discard changes?',
    message: `"${S.doc.filename}" has changes that haven't been downloaded. Opening another file will discard them.`,
    confirmLabel: 'Discard changes',
    danger: true,
  });
}

async function openFile(file, password = null) {
  const previous = S.doc?.id;
  setLoading(`Opening ${file.name}…`);
  try {
    const headers = { 'X-Filename': encodeURIComponent(file.name) };
    if (password) headers['X-Password'] = encodeURIComponent(password);
    const res = await postRaw('/api/open', file, headers);
    if (previous && previous !== res.doc.id) postJSON(`/api/doc/${previous}/close`).catch(() => {});
    loadDocument(res.doc);
  } catch (err) {
    setLoading(false);
    if (err.status === 401) {
      const pw = await dialogs.passwordPrompt(file.name, err.data?.wrong);
      if (pw) return openFile(file, pw);
      return null;
    }
    toast(err.message, { type: 'error' });
  } finally {
    setLoading(false);
  }
  return null;
}

async function openPicker() {
  if (!(await confirmDiscard())) return;
  const [file] = await pickFiles({ accept: 'application/pdf,.pdf,image/*,.xps,.epub,.cbz,.svg' });
  if (file) openFile(file);
}

async function createFromImages(files) {
  let list = files;
  if (!list) list = await pickFiles({ accept: 'image/*', multiple: true });
  if (!list?.length) return;
  if (!(await confirmDiscard())) return;
  const [first, ...rest] = list;
  await openFile(first);
  if (S.doc && rest.length) await pages.insertImages(S.doc.pages.length, rest);
  if (S.doc) $('#doc-filename').value = `${first.name.replace(/\.[^.]+$/, '')}.pdf`;
}

async function createBlank() {
  if (!(await confirmDiscard())) return;
  const res = await postJSON('/api/new', { size: navigator.language === 'en-US' ? 'letter' : 'a4', filename: 'Untitled.pdf' });
  loadDocument(res.doc);
  tools.setTool('text');
}

function updateHeader() {
  const doc = S.doc;
  if (!doc) return;
  $('#btn-undo').disabled = !doc.canUndo;
  $('#btn-redo').disabled = !doc.canRedo;
  $('#btn-undo').dataset.tip = doc.canUndo ? `Undo ${doc.undoLabel.toLowerCase()} (${K}Z)` : 'Nothing to undo';
  $('#btn-redo').dataset.tip = doc.canRedo ? `Redo ${doc.redoLabel.toLowerCase()} (⇧${K}Z)` : 'Nothing to redo';
  $('#page-total').textContent = doc.pages.length;
  $('#page-input').value = S.currentPage + 1;
  $('#dirty-dot').hidden = !doc.dirty;
  $('#btn-save-path').hidden = !doc.hasPath;
  $('#btn-zoom-menu').textContent = `${Math.round(S.zoom * 100)}%`;
}

async function undo() {
  if (!S.doc) return;
  finishAllEditing();
  await flushQueue();
  if (!S.doc.canUndo) return;
  const label = S.doc.undoLabel;
  await mutate('undo').catch(() => {});
  objects.clearSelection();
  toast(`Undid ${label.toLowerCase()}`, { timeout: 1600 });
}

async function redo() {
  if (!S.doc) return;
  finishAllEditing();
  await flushQueue();
  if (!S.doc.canRedo) return;
  await mutate('redo').catch(() => {});
  objects.clearSelection();
}

// ------------------------------------------------------------------ header
function wireHeader() {
  const toggle = $('#btn-panel');
  const paintToggle = () => {
    const open = document.body.classList.contains('panel-open');
    toggle.innerHTML = open ? `${icon('x')}<span>Close</span>` : `${icon('sidebar')}<span>Options</span>`;
  };
  paintToggle();
  new MutationObserver(paintToggle).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  toggle.addEventListener('click', () => setPanel(!document.body.classList.contains('panel-open')));
  $('#panel-scrim').addEventListener('click', () => setPanel(false));
  $('#viewer').addEventListener('pointerdown', () => setPanel(false));
  on('doc', () => setPanel(false));

  $('#btn-open').addEventListener('click', openPicker);
  $('#btn-open-other').addEventListener('click', openPicker);
  $('#btn-from-images').addEventListener('click', () => createFromImages());
  $('#btn-blank').addEventListener('click', createBlank);
  $('#btn-undo').addEventListener('click', undo);
  $('#btn-redo').addEventListener('click', redo);
  $('#btn-zoom-in').addEventListener('click', () => viewer.setZoom(S.zoom * 1.2));
  $('#btn-zoom-out').addEventListener('click', () => viewer.setZoom(S.zoom / 1.2));
  $('#btn-zoom-menu').addEventListener('click', (e) => showMenu(e.currentTarget, [
    { label: 'Fit width', icon: 'fit', shortcut: `${K}0`, onClick: () => viewer.fit('width') },
    { label: 'Fit page', icon: 'fit', onClick: () => viewer.fit('page') },
    '-',
    ...[0.5, 0.75, 1, 1.25, 1.5, 2, 3].map((z) => ({ label: `${z * 100}%`, onClick: () => viewer.setZoom(z) })),
  ]));
  $('#btn-search').addEventListener('click', () => search.openSearch());
  $('#btn-help').addEventListener('click', dialogs.shortcutsDialog);
  $('#btn-download').addEventListener('click', () => dialogs.quickDownload().catch((err) => toast(err.message, { type: 'error' })));
  $('#btn-download-menu').addEventListener('click', (e) => showMenu(e.currentTarget, [
    { label: 'Download', icon: 'download', shortcut: `${K}S`, onClick: () => dialogs.quickDownload() },
    { label: 'Download with options…', icon: 'file-text', onClick: () => dialogs.downloadDialog() },
    { label: 'Compress & download…', icon: 'compress', onClick: () => dialogs.downloadDialog() },
    { label: 'Protect with password…', icon: 'lock', onClick: () => dialogs.downloadDialog({ focus: 'password' }) },
    S.doc?.hasPath ? '-' : null,
    S.doc?.hasPath ? { label: 'Save to original file', icon: 'save', onClick: saveToPath } : null,
  ].filter(Boolean), { align: 'right' }));
  $('#btn-save-path').addEventListener('click', saveToPath);
  const pageInput = $('#page-input');
  pageInput.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      const n = parseInt(pageInput.value, 10);
      if (n >= 1 && n <= S.doc.pages.length) viewer.scrollToPage(n - 1);
      pageInput.blur();
    }
  });
  pageInput.addEventListener('focus', () => pageInput.select());
  const nameInput = $('#doc-filename');
  nameInput.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') nameInput.blur(); });
  nameInput.addEventListener('change', () => {
    if (S.doc) document.title = `${nameInput.value} — PDF Editor`;
  });
}

async function saveToPath() {
  if (!S.doc?.hasPath) return;
  finishAllEditing();
  try {
    const res = await mutate('save', {});
    toast(`Saved to ${res.saved}`, { type: 'success' });
  } catch {
    /* toast shown */
  }
}

// ------------------------------------------------------------------ keyboard
function isTyping(e) {
  const t = e.target;
  return t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName);
}

function wireKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (!S.doc) {
      if (mod(e) && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        openPicker();
      }
      return;
    }
    const key = e.key.toLowerCase();
    if (mod(e)) {
      if (key === 'z' && !isTyping(e)) { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
      if (key === 'y' && !isTyping(e)) { e.preventDefault(); redo(); return; }
      if (key === 's') { e.preventDefault(); finishAllEditing(); S.doc.hasPath ? saveToPath() : dialogs.quickDownload(); return; }
      if (key === 'o') { e.preventDefault(); openPicker(); return; }
      if (key === 'f') { e.preventDefault(); search.openSearch(); return; }
      if (key === 'p') { e.preventDefault(); dialogs.downloadDialog(); return; }
      if (key === '=' || key === '+') { e.preventDefault(); viewer.setZoom(S.zoom * 1.2); return; }
      if (key === '-') { e.preventDefault(); viewer.setZoom(S.zoom / 1.2); return; }
      if (key === '0') { e.preventDefault(); viewer.fit('width'); return; }
      if (isTyping(e)) return;
      if (key === 'd') { e.preventDefault(); objects.duplicateSelected(); return; }
      if (key === 'c') { if (objects.copySelected()) e.preventDefault(); return; }
      if (key === 'a' && S.tool === 'select') {
        const page = S.doc.pages[S.currentPage];
        if (page) { e.preventDefault(); objects.selectAllOnPage(page.id); }
      }
      return;
    }
    if (isTyping(e)) return;
    if (e.key === 'Escape') {
      closeMenu();
      if (S.placing) tools.cancelPlacing();
      else if (S.selection) objects.clearSelection();
      else if (!$('#search-bar').hidden) search.closeSearch();
      else if (S.view === 'pages') pages.closeOrganizer();
      else if (S.tool !== 'select') tools.setTool('select');
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && S.selection) {
      e.preventDefault();
      objects.deleteSelected();
      return;
    }
    if (e.key.startsWith('Arrow') && S.selection) {
      e.preventDefault();
      const step = e.shiftKey ? 10 : 1;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      objects.nudgeSelected(...d);
      return;
    }
    if (e.key === '?' || (e.shiftKey && key === '/')) {
      dialogs.shortcutsDialog();
      return;
    }
    if (e.altKey || e.shiftKey) return;
    const tool = tools.TOOLS.find((t) => t !== '|' && t.key === key);
    if (tool && S.view === 'edit') {
      e.preventDefault();
      tools.setTool(tool.id);
    }
  });

  document.addEventListener('paste', async (e) => {
    if (!S.doc || isTyping(e)) return;
    const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith('image/'));
    if (file) {
      e.preventDefault();
      tools.startImagePlacement(file);
      return;
    }
    const page = S.doc.pages[S.currentPage];
    if (page && objects.paste(page.id)) e.preventDefault();
  });
}

// ------------------------------------------------------------------ pointer
function wirePointer() {
  const container = viewer.pagesContainer;
  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (S.editing?.kind === 'text' && textedit.handlePointerDown(e)) return;
    if (objects.handlePointerDown(e)) return;
    if (S.tool === 'edittext' && textedit.handlePointerDown(e)) return;
    tools.handlePointerDown(e);
  });
  viewer.viewerEl.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.page')) return;
    finishAllEditing();
    objects.clearSelection();
    if (S.placing) tools.cancelPlacing();
  });
}

// ------------------------------------------------------------------ drag & drop
function wireDragDrop() {
  const overlay = $('#drop-overlay');
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth -= 1;
    if (depth <= 0) { overlay.hidden = true; depth = 0; }
  });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    const files = [...e.dataTransfer.files];
    const pdfs = files.filter((f) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name));
    const images = files.filter((f) => f.type.startsWith('image/'));
    if (!S.doc) {
      if (pdfs.length) openFile(pdfs[0]);
      else if (images.length) createFromImages(images);
      return;
    }
    if (pdfs.length) {
      showMenu(null, [
        { header: pdfs[0].name },
        { label: 'Open this PDF', icon: 'folder', onClick: async () => { if (await confirmDiscard()) openFile(pdfs[0]); } },
        { label: 'Insert its pages after the current page', icon: 'files', onClick: () => pages.insertPdf(S.currentPage + 1, pdfs[0]) },
        { label: 'Insert at the end', icon: 'files', onClick: () => pages.insertPdf(S.doc.pages.length, pdfs[0]) },
      ], { x: e.clientX, y: e.clientY });
      return;
    }
    if (images.length === 1) {
      tools.startImagePlacement(images[0]);
    } else if (images.length > 1) {
      showMenu(null, [
        { header: `${images.length} images` },
        { label: 'Insert as new pages after the current page', icon: 'image-plus', onClick: () => pages.insertImages(S.currentPage + 1, images) },
      ], { x: e.clientX, y: e.clientY });
    }
  });
}

// ------------------------------------------------------------------ events
function wireEvents() {
  on('doc', ({ replaceObjects }) => {
    viewer.syncPages();
    if (replaceObjects) {
      if (S.selection && !objects.selectedObjects().length) S.selection = null;
      objects.renderAllObjects();
    }
    updateHeader();
  });
  on('busy', (n) => document.body.classList.toggle('busy', n > 0));
  on('current-page', updateHeader);
  on('zoom', updateHeader);
  on('closed', () => {
    toast('This document is no longer open on the editor server (was it restarted?). Please open it again.', { type: 'error', timeout: 9000 });
    closeDocument();
  });
  on('undo', undo);
  on('set-tool', (id) => tools.setTool(id));
  on('open-search', (opts) => search.openSearch(opts));
  on('open-signature', openSignatureDialog);
  on('redact-search', (q) => search.redactMatches(q).catch((err) => toast(err.message, { type: 'error' })));
  on('clear-redactions', () => {
    const ops = [];
    for (const [pageId, list] of Object.entries(S.doc.objects || {})) {
      const ids = list.filter((o) => o.type === 'redact').map((o) => o.id);
      if (ids.length) ops.push({ op: 'delete', pageId, ids, label: 'Clear redactions' });
    }
    if (ops.length) mutate('objects', { ops });
  });
  on('before-export', finishAllEditing);
  on('before-organizer', finishAllEditing);
  on('organizer-closed', () => requestAnimationFrame(() => {
    if (S.fit) viewer.applyFit();
    viewer.syncPages();
    objects.renderAllObjects();
  }));
  on('tool', () => { if (S.doc) textedit.renderVisibleTextLayers(); });

  window.addEventListener('beforeunload', (e) => {
    if (S.doc?.dirty || pendingCount() > 0) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

// ------------------------------------------------------------------ boot
async function boot() {
  hydrateIcons();
  initTooltips();
  buildToolbar();
  viewer.initViewer();
  objects.initObjects();
  textedit.initTextEditing();
  tools.initTools();
  panel.initPanel();
  pages.initPages();
  search.initSearch();
  initForms();
  wireHeader();
  wireKeyboard();
  wirePointer();
  wireDragDrop();
  wireEvents();
  const dropCard = $('#drop-card');
  ['dragenter', 'dragover'].forEach((ev) => dropCard.addEventListener(ev, () => dropCard.classList.add('over')));
  ['dragleave', 'drop'].forEach((ev) => dropCard.addEventListener(ev, () => dropCard.classList.remove('over')));

  try {
    S.config = await getJSON('/api/config');
  } catch {
    toast('Could not reach the editor server. Is it running?', { type: 'error', timeout: 10000 });
    return;
  }
  if (S.config.appMode) {
    const beat = () => postJSON('/api/heartbeat').catch(() => {});
    beat();
    setInterval(beat, 15000);
  }
  describeServer();
  // a document handed over by the home page, else what this tab was editing before a
  // reload, else the file passed on the command line
  const params = new URLSearchParams(location.search);
  const handover = params.get('doc');
  const action = params.get('do');
  if (handover || action) history.replaceState(null, '', location.pathname);
  for (const id of [handover, rememberedDoc(), S.config.preload]) {
    if (!id) continue;
    try {
      const res = await getJSON(`/api/doc/${id}`);
      loadDocument(res.doc);
      break;
    } catch {
      /* no longer open on the server */
    }
  }
  watchForNewBuild();
  if (action) runHomeAction(action);
  window.pdfEditor = { S, openFile, loadDocument, tools, objects, viewer, mutate };
}

/**
 * Tell people where their file actually goes. On someone's own machine nothing
 * leaves it; on a server it is uploaded, and saying otherwise would be a lie.
 * The source offer belongs here too: the AGPL is owed to whoever uses this over
 * a network, and plenty of them will never see the home page.
 */
function describeServer() {
  const { public: hosted, idleMinutes, sourceUrl } = S.config;
  if (hosted) {
    const kept = idleMinutes
      ? `Your file is uploaded to this server so it can be worked on, is private to you, and is deleted ${idleMinutes} minutes after you stop working on it.`
      : 'Your file is uploaded to this server so it can be worked on, and is private to you.';
    $('#drop-blurb').textContent = 'Change existing text, add text and images, sign, highlight, fill forms, and organize pages.';
    const note = $('#drop-note');
    note.textContent = kept;
    note.hidden = false;
  }
  if (sourceUrl) {
    const note = $('#drop-note');
    note.append(note.textContent ? ' ' : '', Object.assign(document.createElement('a'), {
      href: sourceUrl, textContent: 'Source code', rel: 'noreferrer', target: '_blank',
    }));
    note.hidden = false;
  }
}

// A browser holding yesterday's scripts behaves in ways nobody can explain and no
// amount of reloading fixes, because the reload is served from cache too.  The page
// knows which build it was given; if the server has a newer one, say so.
function watchForNewBuild() {
  const mine = document.querySelector('meta[name="build"]')?.content;
  if (!mine) return;
  const check = async () => {
    try {
      const cfg = await getJSON('/api/config');
      if (String(cfg.build || '') && String(cfg.build) !== mine) {
        toast('A newer version of the editor is available.', {
          timeout: 15 * 60 * 1000,  // 0 would mean "dismiss immediately"

          action: () => location.reload(),
          actionLabel: 'Reload',
        });
        return true;
      }
    } catch {
      /* offline or asleep: ask again later */
    }
    return false;
  };
  setTimeout(async () => { if (!(await check())) setInterval(check, 10 * 60 * 1000); }, 20000);
}

// What each card on the home page asks the editor to do once it is up.
function runHomeAction(id) {
  const at = () => S.doc?.pages.length ?? 0;
  const actions = {
    forms: () => {
      tools.setTool('select');
      toast('Click any field on the page to fill it in.', { timeout: 6000 });
    },
    findreplace: () => search.openSearch({ replace: true }),
    merge: () => pages.insertPdf(at()),
    organize: () => pages.openOrganizer(),
    insertimages: () => pages.insertImages(at()),
    split: dialogs.splitDialog,
    extract: dialogs.extractDialog,
    crop: dialogs.cropDialog,
    ocr: dialogs.ocrDialog,
    exportimages: dialogs.exportImagesDialog,
    extracttext: dialogs.extractText,
    extractimages: dialogs.extractImages,
    compress: () => dialogs.downloadDialog(),
    password: () => dialogs.downloadDialog({ focus: 'password' }),
    watermark: dialogs.watermarkDialog,
    pagenumbers: dialogs.pageNumbersDialog,
    metadata: dialogs.metadataDialog,
    images: () => createFromImages(),
    blank: () => createBlank(),
  };
  const run = actions[id] || (tools.toolById(id) ? () => tools.setTool(id) : null);
  if (!run) return;
  if (!S.doc && !['images', 'blank'].includes(id)) return;  // nothing opened: leave it be
  setTimeout(run, 80);  // after the first page has drawn
}

boot();
