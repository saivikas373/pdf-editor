// One page per job.  Each task says what files it takes, what options it offers and
// what to do with them; the engine below draws the page and runs it.  Nothing here
// opens the editor unless the job genuinely needs hands on the document.
import { icon } from './icons.js';

const H = { 'X-PDF-Editor': '1' };

// ----------------------------------------------------------------- the server
let maxUploadMb = 0;
fetch('/api/config', { headers: H }).then((r) => r.json()).then((c) => { maxUploadMb = c.maxUploadMb || 0; }).catch(() => {});

async function openDoc(file, password) {
  if (maxUploadMb && file.size > maxUploadMb * 1024 * 1024) {
    throw new Error(`${file.name} is ${Math.round(file.size / 1024 / 1024)} MB. The limit here is ${maxUploadMb} MB.`);
  }
  const headers = { ...H, 'X-Filename': encodeURIComponent(file.name) };
  if (password) headers['X-Password'] = encodeURIComponent(password);
  const res = await fetch('/api/open', { method: 'POST', headers, body: file });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    const pw = window.prompt(`${file.name} is password protected. Enter its password:`);
    if (!pw) throw new Error('This file needs a password.');
    return openDoc(file, pw);
  }
  if (!res.ok) throw new Error(data.error || `Could not open ${file.name}.`);
  return data.doc;
}

async function post(docId, path, body) {
  const res = await fetch(`/api/doc/${docId}/${path}`, {
    method: 'POST',
    headers: { ...H, 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}

async function postRaw(docId, path, body, extra) {
  const res = await fetch(`/api/doc/${docId}/${path}`, { method: 'POST', headers: { ...H, ...extra }, body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}

async function fetchFile(docId, path, body) {
  const res = await fetch(`/api/doc/${docId}/${path}`, {
    method: 'POST',
    headers: { ...H, 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || 'Something went wrong.');
  }
  const disp = res.headers.get('Content-Disposition') || '';
  const match = /filename\*=UTF-8''([^;]+)|filename="([^"]+)"/.exec(disp);
  const name = decodeURIComponent(match?.[1] || match?.[2] || 'download');
  return { blob: await res.blob(), filename: name };
}

// merging, images to PDF and anything else that starts from one file and adds more
async function combine(files, status, insert) {
  const first = await openDoc(files[0]);
  let pages = first.pages.length;
  for (let i = 1; i < files.length; i += 1) {
    status(`Adding ${files[i].name} (${i + 1} of ${files.length})…`);
    const res = await insert(first.id, files[i], pages);
    pages = res.doc.pages.length;
  }
  return { docId: first.id, pages };
}

// ------------------------------------------------------------------ the tasks
const TASKS = {
  merge: {
    title: 'Merge PDFs',
    blurb: 'Add the files you want joined, drag them into the order you want, and merge. Nothing else changes about them.',
    accept: 'application/pdf,.pdf', multiple: true, min: 2, reorder: true,
    action: 'Merge PDFs',
    done: 'Merged',
    async run({ files, status }) {
      const { docId, pages } = await combine(files, status, (id, file, at) =>
        postRaw(id, 'pages/insert-pdf', file, { 'X-Filename': encodeURIComponent(file.name), 'X-At': String(at) }));
      status('Writing the merged file…');
      const out = await fetchFile(docId, 'export', { filename: 'merged.pdf', markSaved: true });
      return { ...out, docId, note: `${files.length} files, ${pages} pages.` };
    },
  },

  images: {
    title: 'Images to PDF',
    blurb: 'Every picture becomes one page, in the order you set here.',
    accept: 'image/*', multiple: true, min: 1, reorder: true,
    action: 'Make the PDF',
    done: 'Made your PDF',
    options: [
      { id: 'pageSize', type: 'seg', label: 'Page size', value: 'fit',
        choices: [['fit', 'Fit each image'], ['a4', 'A4'], ['letter', 'Letter']] },
    ],
    async run({ files, opts, status }) {
      const first = await openDoc(files[0]);
      let pages = first.pages.length;
      for (let i = 1; i < files.length; i += 1) {
        status(`Adding ${files[i].name} (${i + 1} of ${files.length})…`);
        const asset = await postRaw(first.id, 'assets', files[i], { 'Content-Type': files[i].type || 'image/png' });
        const res = await post(first.id, 'pages/insert-images', { assets: [asset.asset], at: pages, pageSize: opts.pageSize });
        pages = res.doc.pages.length;
      }
      status('Writing the PDF…');
      const out = await fetchFile(first.id, 'export', { filename: `${files[0].name.replace(/\.[^.]+$/, '')}.pdf` });
      return { ...out, docId: first.id, note: `${pages} page${pages === 1 ? '' : 's'}.` };
    },
  },

  split: {
    title: 'Split a PDF',
    blurb: 'Break one file into several. They come back together in a ZIP.',
    accept: 'application/pdf,.pdf',
    action: 'Split it',
    done: 'Split',
    options: [
      { id: 'mode', type: 'seg', label: 'How', value: 'each',
        choices: [['each', 'One file per page'], ['every', 'Every N pages'], ['ranges', 'By page ranges']] },
      { id: 'value', type: 'text', label: 'Pages per file', value: '2', showFor: { mode: 'every' } },
      { id: 'ranges', type: 'text', label: 'Ranges', value: '1-3, 4-6', hint: 'Comma separated, e.g. 1-3, 4-6, 7', showFor: { mode: 'ranges' } },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Splitting…');
      const value = opts.mode === 'ranges' ? opts.ranges : opts.value;
      const out = await fetchFile(doc.id, 'split', { mode: opts.mode, value });
      return { ...out, docId: doc.id, note: `From ${doc.pages.length} pages.` };
    },
  },

  extractpages: {
    title: 'Extract pages',
    blurb: 'Pull a few pages out into a PDF of their own. The original is untouched.',
    accept: 'application/pdf,.pdf',
    action: 'Extract',
    done: 'Extracted',
    options: [{ id: 'pages', type: 'text', label: 'Pages', value: '1-3', hint: 'e.g. 1-3, 7, 10-12' }],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Extracting…');
      const out = await fetchFile(doc.id, 'extract', { pages: opts.pages });
      return { ...out, docId: doc.id, note: `Pages ${opts.pages} of ${doc.pages.length}.` };
    },
  },

  deletepages: {
    title: 'Delete pages',
    blurb: 'Take pages out and download what is left.',
    accept: 'application/pdf,.pdf',
    action: 'Delete them',
    done: 'Pages deleted',
    options: [{ id: 'pages', type: 'text', label: 'Pages to remove', value: '1', hint: 'e.g. 2, 5-7' }],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      const keep = pageSpec(opts.pages, doc.pages.length);
      if (!keep.length) throw new Error('No pages matched that.');
      if (keep.length >= doc.pages.length) throw new Error('That would delete every page.');
      status('Removing…');
      await post(doc.id, 'pages/delete', { pageIds: keep.map((i) => doc.pages[i].id) });
      const out = await fetchFile(doc.id, 'export', {});
      return { ...out, docId: doc.id, note: `${doc.pages.length - keep.length} pages left.` };
    },
  },

  rotate: {
    title: 'Rotate pages',
    blurb: 'Turn pages the right way up and download the result.',
    accept: 'application/pdf,.pdf',
    action: 'Rotate',
    done: 'Rotated',
    options: [
      { id: 'angle', type: 'seg', label: 'Turn', value: '90',
        choices: [['90', 'Right 90°'], ['-90', 'Left 90°'], ['180', 'Upside down']] },
      { id: 'pages', type: 'text', label: 'Pages', value: 'all', hint: '"all", or e.g. 1-3, 7' },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      const idx = /^all$/i.test(opts.pages.trim()) ? doc.pages.map((_, i) => i) : pageSpec(opts.pages, doc.pages.length);
      if (!idx.length) throw new Error('No pages matched that.');
      status('Rotating…');
      await post(doc.id, 'pages/rotate', { pageIds: idx.map((i) => doc.pages[i].id), angle: Number(opts.angle) });
      const out = await fetchFile(doc.id, 'export', {});
      return { ...out, docId: doc.id, note: `${idx.length} page${idx.length === 1 ? '' : 's'} turned.` };
    },
  },

  compress: {
    title: 'Compress a PDF',
    blurb: 'Make the file smaller by re-encoding the pictures inside it. Text stays sharp.',
    accept: 'application/pdf,.pdf',
    action: 'Compress',
    done: 'Compressed',
    options: [
      { id: 'compress', type: 'seg', label: 'How small', value: 'balanced',
        choices: [['balanced', 'Smaller'], ['strong', 'Smallest']] },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Compressing…');
      const out = await fetchFile(doc.id, 'export', { compress: opts.compress });
      const before = files[0].size;
      const after = out.blob.size;
      const saved = Math.max(0, Math.round((1 - after / before) * 100));
      return { ...out, docId: doc.id, note: `${fmtSize(before)} → ${fmtSize(after)} (${saved}% smaller).` };
    },
  },

  protect: {
    title: 'Protect with a password',
    blurb: 'AES-256 encryption, and you decide what a reader is allowed to do.',
    accept: 'application/pdf,.pdf',
    action: 'Protect it',
    done: 'Protected',
    options: [
      { id: 'password', type: 'password', label: 'Password' },
      { id: 'repeat', type: 'password', label: 'Repeat the password' },
      { id: 'print', type: 'check', label: 'Allow printing', value: true },
      { id: 'copy', type: 'check', label: 'Allow copying text', value: true },
      { id: 'modify', type: 'check', label: 'Allow editing', value: false },
    ],
    async run({ files, opts, status }) {
      if (!opts.password) throw new Error('Enter a password.');
      if (opts.password !== opts.repeat) throw new Error("The two passwords don't match.");
      const doc = await openDoc(files[0]);
      status('Encrypting…');
      const out = await fetchFile(doc.id, 'export', {
        password: opts.password,
        permissions: { print: opts.print, copy: opts.copy, modify: opts.modify, annotate: opts.modify },
      });
      return { ...out, docId: doc.id, note: 'Keep the password somewhere safe — the file cannot be opened without it.' };
    },
  },

  pdf2img: {
    title: 'PDF to images',
    blurb: 'Every page becomes a picture. One page comes back on its own, several come back as a ZIP.',
    accept: 'application/pdf,.pdf',
    action: 'Make images',
    done: 'Exported',
    options: [
      { id: 'format', type: 'seg', label: 'Format', value: 'png', choices: [['png', 'PNG'], ['jpg', 'JPEG']] },
      { id: 'dpi', type: 'seg', label: 'Resolution', value: '150',
        choices: [['96', 'Screen (96)'], ['150', 'Good (150)'], ['300', 'Print (300)']] },
      { id: 'pages', type: 'text', label: 'Pages', value: 'all', hint: '"all", or e.g. 1-3, 7' },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Rendering…');
      const pages = /^all$/i.test(opts.pages.trim()) ? '' : opts.pages;
      const out = await fetchFile(doc.id, 'export-images', { format: opts.format, dpi: Number(opts.dpi), pages });
      return { ...out, docId: doc.id, note: `${opts.format.toUpperCase()} at ${opts.dpi} dpi.` };
    },
  },

  extracttext: {
    title: 'Extract the text',
    blurb: 'Everything the document says, as a plain .txt file.',
    accept: 'application/pdf,.pdf',
    action: 'Extract the text',
    done: 'Extracted',
    async run({ files, status }) {
      const doc = await openDoc(files[0]);
      status('Reading…');
      const out = await fetchFile(doc.id, 'extract-text', {});
      const words = (await out.blob.text()).trim().split(/\s+/).filter(Boolean).length;
      return { ...out, docId: doc.id, note: `${words.toLocaleString()} words from ${doc.pages.length} pages.` };
    },
  },

  extractimages: {
    title: 'Pull out the pictures',
    blurb: 'Every image embedded in the PDF, at its original resolution, in a ZIP.',
    accept: 'application/pdf,.pdf',
    action: 'Pull them out',
    done: 'Got the pictures',
    async run({ files, status }) {
      const doc = await openDoc(files[0]);
      status('Looking through the pages…');
      const out = await fetchFile(doc.id, 'extract-images', {});
      return { ...out, docId: doc.id };
    },
  },

  ocr: {
    title: 'Make a scan searchable',
    blurb: 'Recognises the text in a scanned page and lays it over the picture, so the file can be searched, copied — and edited in the editor.',
    accept: 'application/pdf,.pdf,image/*',
    action: 'Recognise the text',
    done: 'Recognised',
    options: [
      { id: 'language', type: 'select', label: 'Language', value: 'eng',
        choices: [['eng', 'English'], ['deu', 'German'], ['fra', 'French'], ['spa', 'Spanish'], ['ita', 'Italian'], ['por', 'Portuguese'], ['nld', 'Dutch']] },
      { id: 'pages', type: 'text', label: 'Pages', value: 'all', hint: '"all", or e.g. 1-3' },
      { id: 'force', type: 'check', label: 'Redo pages that already have text', value: false },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Recognising text — this can take a moment per page…');
      const pages = /^all$/i.test(opts.pages.trim()) ? '' : opts.pages;
      const res = await post(doc.id, 'tools/ocr', { pages, language: opts.language, force: opts.force });
      const done = res.result?.ocr?.length ?? 0;
      const out = await fetchFile(doc.id, 'export', {});
      return { ...out, docId: doc.id, note: `${done} page${done === 1 ? '' : 's'} recognised.` };
    },
  },

  watermark: {
    title: 'Add a watermark',
    blurb: 'Text across every page — a draft stamp, a copyright line, whatever you need.',
    accept: 'application/pdf,.pdf',
    action: 'Add the watermark',
    done: 'Watermarked',
    options: [
      { id: 'text', type: 'text', label: 'Text', value: 'CONFIDENTIAL' },
      { id: 'mode', type: 'seg', label: 'Layout', value: 'center', choices: [['center', 'Once, across the middle'], ['tile', 'Tiled']] },
      { id: 'layer', type: 'seg', label: 'Depth', value: 'over', choices: [['over', 'Over the content'], ['under', 'Behind it']] },
      { id: 'size', type: 'number', label: 'Size', value: 60 },
      { id: 'angle', type: 'number', label: 'Angle', value: 45 },
      { id: 'opacity', type: 'seg', label: 'Strength', value: '0.25',
        choices: [['0.12', 'Faint'], ['0.25', 'Light'], ['0.5', 'Strong'], ['0.85', 'Solid']] },
      { id: 'color', type: 'color', label: 'Colour', value: '#dc2626' },
    ],
    async run({ files, opts, status }) {
      if (!opts.text.trim()) throw new Error('Type the watermark text.');
      const doc = await openDoc(files[0]);
      status('Stamping every page…');
      await post(doc.id, 'tools/watermark', {
        text: opts.text, size: Number(opts.size), color: opts.color, opacity: Number(opts.opacity),
        angle: Number(opts.angle), mode: opts.mode, layer: opts.layer, pages: '',
      });
      const out = await fetchFile(doc.id, 'export', {});
      return { ...out, docId: doc.id, note: `On all ${doc.pages.length} pages.` };
    },
  },

  pagenumbers: {
    title: 'Page numbers & headers',
    blurb: 'Numbering, headers and footers from a template. {n} is the page, {total} the count, {date} today, {filename} the file.',
    accept: 'application/pdf,.pdf',
    action: 'Add them',
    done: 'Added',
    options: [
      { id: 'format', type: 'text', label: 'Template', value: 'Page {n} of {total}' },
      { id: 'position', type: 'select', label: 'Where', value: 'bottom-center',
        choices: [['bottom-center', 'Bottom centre'], ['bottom-left', 'Bottom left'], ['bottom-right', 'Bottom right'],
          ['top-center', 'Top centre'], ['top-left', 'Top left'], ['top-right', 'Top right']] },
      { id: 'start', type: 'number', label: 'Start at', value: 1 },
      { id: 'size', type: 'number', label: 'Font size', value: 10 },
      { id: 'margin', type: 'number', label: 'Margin', value: 28 },
      { id: 'color', type: 'color', label: 'Colour', value: '#333333' },
    ],
    async run({ files, opts, status }) {
      const doc = await openDoc(files[0]);
      status('Numbering…');
      await post(doc.id, 'tools/stamp', {
        format: opts.format, position: opts.position, start: Number(opts.start),
        size: Number(opts.size), color: opts.color, margin: Number(opts.margin), pages: '',
      });
      const out = await fetchFile(doc.id, 'export', {});
      return { ...out, docId: doc.id, note: `On all ${doc.pages.length} pages.` };
    },
  },
};

// ------------------------------------------------------------------- helpers
function pageSpec(spec, count) {
  const out = new Set();
  for (const part of String(spec || '').split(',')) {
    const range = /^\s*(\d+)\s*-\s*(\d+)\s*$/.exec(part);
    const one = /^\s*(\d+)\s*$/.exec(part);
    if (range) {
      const [a, b] = [Number(range[1]), Number(range[2])].sort((x, y) => x - y);
      for (let i = a; i <= b; i += 1) if (i >= 1 && i <= count) out.add(i - 1);
    } else if (one && Number(one[1]) >= 1 && Number(one[1]) <= count) {
      out.add(Number(one[1]) - 1);
    }
  }
  return [...out].sort((a, b) => a - b);
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  node.append(...kids.filter(Boolean));
  return node;
}

// ---------------------------------------------------------------- the engine
const task = TASKS[new URLSearchParams(location.search).get('t') || location.pathname.split('/').pop()];
const main = document.querySelector('#task-body');
let files = [];
let values = {};

if (!task) {
  document.querySelector('#task-title').textContent = 'Tool not found';
  document.querySelector('#task-blurb').textContent = 'That tool does not exist. Everything we do have is on the home page.';
} else {
  document.title = `${task.title} — PDF Editor`;
  document.querySelector('#task-title').textContent = task.title;
  document.querySelector('#task-blurb').textContent = task.blurb;
  values = Object.fromEntries((task.options || []).map((o) => [o.id, o.value ?? (o.type === 'check' ? false : '')]));
  render();
}

function pick() {
  const input = el('input', { type: 'file', accept: task.accept, multiple: task.multiple ? '' : null });
  input.style.display = 'none';
  input.addEventListener('change', () => {
    add([...input.files]);
    input.remove();
  });
  document.body.append(input);
  input.click();
}

function add(list) {
  if (!list.length) return;
  files = task.multiple ? [...files, ...list] : [list[0]];
  render();
}

function render(result) {
  main.replaceChildren();
  if (result) {
    main.append(resultCard(result));
    return;
  }
  main.append(filesView());
  if (files.length) {
    main.append(optionsView(), runView());
  }
}

function filesView() {
  if (!files.length) {
    const drop = el('div', { class: 'drop', on: { click: pick } },
      el('span', { class: 'drop-icon', html: icon('upload') }),
      el('div', { class: 'drop-text' },
        el('strong', { text: task.multiple ? 'Choose your files' : 'Choose your file' }),
        el('span', { text: 'or drop them anywhere on this page' })));
    return drop;
  }
  const rows = files.map((file, i) => {
    const row = el('div', { class: 'file-row', draggable: task.reorder ? 'true' : null },
      task.reorder ? el('span', { class: 'file-grip', html: icon('move') }) : null,
      el('span', { class: 'file-num', text: String(i + 1) }),
      el('span', { class: 'file-name', text: file.name }),
      el('span', { class: 'file-size', text: fmtSize(file.size) }),
      el('button', { class: 'file-drop-x', 'aria-label': `Remove ${file.name}`, html: icon('x'),
        on: { click: () => { files.splice(i, 1); render(); } } }));
    if (task.reorder) {
      row.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(i)); row.classList.add('dragging'); });
      row.addEventListener('dragend', () => row.classList.remove('dragging'));
      row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drop-target'); });
      row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.classList.remove('drop-target');
        const from = Number(e.dataTransfer.getData('text/plain'));
        if (Number.isNaN(from) || from === i) return;
        const [moved] = files.splice(from, 1);
        files.splice(i, 0, moved);
        render();
      });
    }
    return row;
  });
  const wrap = el('div', {}, el('div', { class: 'files' }, ...rows));
  wrap.append(el('button', { class: 'btn ghost add-more', text: task.multiple ? '+ Add more files' : 'Choose a different file', on: { click: pick } }));
  return wrap;
}

function optionsView() {
  const box = el('div', { class: 'options' });
  for (const opt of task.options || []) {
    if (opt.showFor && values[Object.keys(opt.showFor)[0]] !== Object.values(opt.showFor)[0]) continue;
    const id = `opt-${opt.id}`;
    let control;
    if (opt.type === 'seg') {
      control = el('div', { class: 'seg' });
      for (const [value, label] of opt.choices) {
        control.append(el('button', { type: 'button', class: values[opt.id] === value ? 'on' : '', text: label,
          on: { click: () => { values[opt.id] = value; render(); } } }));
      }
    } else if (opt.type === 'select') {
      control = el('select', { id, on: { change: (e) => { values[opt.id] = e.target.value; } } });
      for (const [value, label] of opt.choices) {
        const o = el('option', { value, text: label });
        if (values[opt.id] === value) o.selected = true;
        control.append(o);
      }
    } else if (opt.type === 'check') {
      const cb = el('input', { type: 'checkbox', id, on: { change: (e) => { values[opt.id] = e.target.checked; } } });
      cb.checked = Boolean(values[opt.id]);
      box.append(el('div', { class: 'opt inline' }, cb, el('label', { for: id, text: opt.label })));
      continue;
    } else {
      control = el('input', {
        id, type: opt.type === 'number' ? 'number' : opt.type === 'password' ? 'password' : opt.type === 'color' ? 'color' : 'text',
        value: values[opt.id] ?? '',
        on: { input: (e) => { values[opt.id] = e.target.value; } },
      });
    }
    box.append(el('div', { class: 'opt' }, el('label', { for: id, text: opt.label }), control,
      opt.hint ? el('span', { class: 'hint', text: opt.hint }) : null));
  }
  return box;
}

function runView() {
  const status = el('span', { class: 'status' });
  const button = el('button', { class: 'btn primary', text: task.action });
  const enough = files.length >= (task.min || 1);
  if (!enough) {
    button.disabled = true;
    status.textContent = `Add ${task.min - files.length} more file${task.min - files.length === 1 ? '' : 's'}.`;
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    status.classList.remove('error');
    status.textContent = 'Working…';
    try {
      const out = await task.run({ files, opts: values, status: (t) => { status.textContent = t; } });
      const url = URL.createObjectURL(out.blob);
      const link = el('a', { href: url, download: out.filename });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      render({ ...out, url });
    } catch (err) {
      status.classList.add('error');
      status.textContent = err.message || 'Something went wrong.';
      button.disabled = false;
    }
  });
  return el('div', { class: 'run-row' }, button, status);
}

function resultCard(out) {
  const url = URL.createObjectURL(out.blob);
  return el('div', { class: 'result' },
    el('h2', { text: `${task.done}. Your download has started.` }),
    el('p', { text: [out.filename, out.note].filter(Boolean).join(' · ') }),
    el('div', { class: 'row' },
      el('a', { class: 'btn primary', href: url, download: out.filename, text: 'Download again' }),
      out.docId ? el('a', { class: 'btn secondary', href: `/editor?doc=${out.docId}`, text: 'Open in the editor' }) : null,
      el('button', { class: 'btn ghost', text: 'Do another', on: { click: () => { files = []; render(); } } }),
      el('a', { class: 'btn ghost', href: '/', text: 'All tools' })));
}

// files can be dropped anywhere on the page
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
  e.preventDefault();
  if (task) add([...(e.dataTransfer?.files || [])]);
});

for (const node of document.querySelectorAll('[data-icon]')) node.innerHTML = icon(node.dataset.icon);
