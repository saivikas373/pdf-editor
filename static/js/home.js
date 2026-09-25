// Home page: every tool, each one a way into the job it does.
//
// A self-contained job (merging, splitting, compressing) has a page of its own at
// /t/<id> with only its own options on it.  Anything that needs eyes on the document
// — signing, redacting, editing the text — opens the editor with that tool running.
import { icon } from './icons.js';

// Jobs with a page of their own.  Everything else genuinely needs the editor —
// you cannot sign or redact a document without looking at it.
const TASK_PAGES = new Set([
  'merge', 'images', 'split', 'extractpages', 'deletepages', 'rotate', 'compress',
  'protect', 'pdf2img', 'extracttext', 'extractimages', 'ocr', 'watermark', 'pagenumbers',
]);

const GROUPS = [
  {
    id: 'edit',
    title: 'Edit a PDF',
    note: 'Work on the document itself — including the text that is already in it.',
    items: [
      ['edittext', 'Edit text', 'edittext', 'Change the words already in the PDF. The font, size and colour are matched, even on scans.'],
      ['text', 'Add text', 'text', 'Drop a text box anywhere and type.'],
      ['forms', 'Fill a form', 'symbol', 'Type into fields, tick boxes and pick from dropdowns — or stamp ✓ ✕ ● onto a flat form.'],
      ['signature', 'Sign', 'signature', 'Draw, type or upload a signature and place it. Saved for next time.'],
      ['image', 'Add an image', 'image', 'Put a picture, logo or stamp on the page.'],
      ['markup', 'Highlight', 'highlight', 'Highlight, underline or strike through any text.'],
      ['draw', 'Draw', 'draw', 'Freehand pen and marker.'],
      ['shape', 'Shapes', 'shape', 'Rectangles, ellipses, lines and arrows.'],
      ['note', 'Comment', 'note', 'Sticky-note comments for whoever reads it next.'],
      ['findreplace', 'Find & replace', 'replace', 'Replace a word across the whole document, keeping its formatting.'],
      ['whiteout', 'Whiteout', 'whiteout', 'Cover anything with a solid box.'],
      ['redact', 'Redact', 'redact', 'Permanently remove what is under the box — not just paint over it.'],
    ],
  },
  {
    id: 'pages',
    title: 'Pages',
    note: 'Put documents together, take them apart, and set the pages in order.',
    items: [
      ['merge', 'Merge PDFs', 'files', 'Add as many PDFs as you like, drag them into order, get one file back.'],
      ['organize', 'Organize pages', 'grid', 'Reorder by dragging, rotate, delete, duplicate or insert blank pages.'],
      ['split', 'Split a PDF', 'scissors', 'Break one file into several, downloaded together as a ZIP.'],
      ['extractpages', 'Extract pages', 'extract', 'Pull a range of pages out into a new PDF.'],
      ['deletepages', 'Delete pages', 'trash', 'Take pages out and keep the rest.'],
      ['rotate', 'Rotate pages', 'redo', 'Turn pages the right way up.'],
      ['crop', 'Crop pages', 'crop', 'Trim the margins, on one page or all of them.'],
      ['insertimages', 'Insert images as pages', 'image', 'Add photos or scans into the document you have open.'],
    ],
  },
  {
    id: 'convert',
    title: 'Create & convert',
    note: 'Start something new, or turn a file into another shape.',
    items: [
      ['images', 'Images to PDF', 'image', 'Turn JPGs or PNGs into a PDF, one page each.', 'images'],
      ['blank', 'Blank document', 'plus', 'Start from an empty page and write on it.', 'none'],
      ['ocr', 'Make a scan searchable', 'ocr', 'Recognise the text in a scanned page so it can be searched and edited.'],
      ['pdf2img', 'PDF to images', 'image', 'Export the pages as PNG or JPG.'],
      ['extracttext', 'Extract the text', 'text', 'Save everything the document says as a .txt file.'],
      ['extractimages', 'Pull out the pictures', 'image', 'Save every image embedded in the PDF.'],
    ],
  },
  {
    id: 'finish',
    title: 'Finish & share',
    note: 'The last pass before the file leaves your hands.',
    items: [
      ['compress', 'Compress & download', 'compress', 'Make the file smaller by re-encoding the images inside it.'],
      ['protect', 'Protect with a password', 'lock', 'AES-256, with control over printing and copying.'],
      ['watermark', 'Add a watermark', 'droplet', 'Text across the page, tiled or single, at the opacity you choose.'],
      ['pagenumbers', 'Page numbers & headers', 'hash', 'Numbering, headers and footers from a template.'],
      ['metadata', 'Document properties', 'info', 'Title, author, subject and keywords.'],
    ],
  },
];

const $ = (sel) => document.querySelector(sel);
const busy = $('#busy');
const note = $('#hero-note');

function setBusy(text) {
  if (!text) {
    busy.hidden = true;
    return;
  }
  $('#busy-text').textContent = text;
  busy.hidden = false;
}

function say(message, isError = false) {
  note.textContent = message || '';
  note.classList.toggle('error', Boolean(isError));
}

function goToEditor(docId, action) {
  const params = new URLSearchParams();
  if (docId) params.set('doc', docId);
  if (action) params.set('do', action);
  try {
    if (docId) sessionStorage.setItem('pdfeditor.docId', docId);
  } catch {
    /* private window: the id in the URL still gets us there */
  }
  window.location.href = `/editor${params.toString() ? `?${params}` : ''}`;
}

function pickFile(accept, multiple = false) {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = accept;
    input.multiple = multiple;
    input.style.display = 'none';
    input.addEventListener('change', () => {
      resolve(multiple ? [...input.files] : input.files[0] || null);
      input.remove();
    });
    document.body.append(input);
    input.click();
  });
}

async function upload(file, action) {
  if (tooBig(file)) return;
  setBusy(`Opening ${file.name}…`);
  say('');
  try {
    const res = await fetch('/api/open', {
      method: 'POST',
      headers: { 'X-PDF-Editor': '1', 'X-Filename': encodeURIComponent(file.name) },
      body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {  // the editor asks for the password itself
      goToEditor(null, action);
      return;
    }
    if (!res.ok) throw new Error(data.error || `Could not open ${file.name}.`);
    goToEditor(data.doc.id, action);
  } catch (err) {
    setBusy(null);
    say(err.message || 'Could not open that file.', true);
  }
}

let maxUploadMb = 0;

function tooBig(file) {
  if (!maxUploadMb || file.size <= maxUploadMb * 1024 * 1024) return false;
  say(`${file.name} is ${Math.round(file.size / 1024 / 1024)} MB. The limit here is ${maxUploadMb} MB.`, true);
  return true;
}

async function start(item) {
  const [id, , , , needs] = item;
  if (TASK_PAGES.has(id)) {
    window.location.href = `/t/${id}`;  // its own page, with only its own options
    return;
  }
  if (needs === 'none') return goToEditor(null, id);
  const file = await pickFile('application/pdf,.pdf,image/*,.xps,.epub,.cbz,.svg');
  if (file) await upload(file, id);
}

function card(item) {
  const [id, title, iconName, desc] = item;
  const el = document.createElement('button');
  el.className = 'card';
  el.dataset.action = id;
  el.innerHTML = `<span class="card-icon">${icon(iconName)}</span>
    <span class="card-body"><span class="card-title"></span><span class="card-desc"></span></span>`;
  el.querySelector('.card-title').textContent = title;
  el.querySelector('.card-desc').textContent = desc;
  el.addEventListener('click', () => {
    if (el.dataset.disabled === '1') return;
    start(item);
  });
  return el;
}

function build() {
  const host = $('#features');
  for (const group of GROUPS) {
    const section = document.createElement('section');
    section.className = 'group';
    section.id = group.id;
    const heading = document.createElement('h2');
    heading.textContent = group.title;
    const sub = document.createElement('p');
    sub.className = 'group-note';
    sub.textContent = group.note;
    const cards = document.createElement('div');
    cards.className = 'cards';
    cards.append(...group.items.map(card));
    section.append(heading, sub, cards);
    host.append(section);
  }
  for (const el of document.querySelectorAll('[data-icon]')) {
    el.innerHTML = icon(el.dataset.icon);
  }
}

function wireDropping() {
  const drop = $('#drop');
  drop.addEventListener('click', (e) => {
    if (e.target.id !== 'btn-choose') start(['edittext']);
  });
  $('#btn-choose').addEventListener('click', () => start(['edittext']));

  let depth = 0;
  window.addEventListener('dragenter', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    depth += 1;
    drop.classList.add('over');
  });
  window.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) drop.classList.remove('over');
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', async (e) => {
    e.preventDefault();
    depth = 0;
    drop.classList.remove('over');
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    const images = files.filter((f) => f.type.startsWith('image/'));
    if (images.length > 1) {  // several pictures: make a PDF of them
      window.location.href = '/t/images';
      return;
    }
    await upload(files[0], 'edittext');
  });
}

async function checkServer() {
  try {
    const config = await fetch('/api/config', { headers: { 'X-PDF-Editor': '1' } }).then((r) => r.json());
    if (!config.ocr?.available) {
      const ocr = document.querySelector('.card[data-action="ocr"]');
      if (ocr) {
        ocr.dataset.disabled = '1';
        ocr.querySelector('.card-desc').textContent = 'Needs Tesseract installed on this machine.';
      }
    }
    maxUploadMb = config.maxUploadMb || 0;
    if (config.sourceUrl) {  // AGPL: people using this over a network may have the source
      const p = document.querySelector('.home-footer p');
      p.append(' Source code: ', Object.assign(document.createElement('a'), { href: config.sourceUrl, textContent: config.sourceUrl }));
    }
    if (config.public) {  // people arriving here deserve to know where their file goes
      const kept = config.idleMinutes ? `, and deleted ${config.idleMinutes} minutes after you stop working on it` : '';
      const p = document.createElement('p');
      p.textContent = `Files are uploaded to this server so they can be worked on. They are private to you${kept}. Nothing is shared, sold or used for anything else.`;
      document.querySelector('.home-footer').prepend(p);
    }
    $('#footer-note').textContent = `Version ${config.version} · ${config.fonts.length} fonts available for matching`;
  } catch {
    say('Could not reach the editor server. Is it running?', true);
  }
}

build();
wireDropping();
checkServer();
