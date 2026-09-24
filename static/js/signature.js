// Signature creation: draw, type or upload; saved locally for reuse.
import { S } from './state.js';
import { h, toast, readAsDataURL, loadImage } from './util.js';
import { icon } from './icons.js';
import { openDialog } from './dialogs.js';
import { uploadImage, beginPlacing } from './tools.js';

const STORE_KEY = 'pdfeditor.signatures';
const SCRIPT_FONTS = ['Snell Roundhand', 'Bradley Hand', 'Brush Script MT', 'Caveat', 'Noteworthy', 'Apple Chancery', 'Savoye LET', 'Segoe Script'];

function loadSaved() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
  } catch {
    return [];
  }
}

function storeSaved(list) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(list.slice(0, 8)));
  } catch {
    toast('Could not save the signature for next time (storage is full).');
  }
}

/** Crop a canvas to its non-transparent content. */
function trimCanvas(canvas, pad = 8) {
  const ctx = canvas.getContext('2d');
  const { width, height } = canvas;
  const data = ctx.getImageData(0, 0, width, height).data;
  let x0 = width; let y0 = height; let x1 = -1; let y1 = -1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > 8) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < 0) return null;
  x0 = Math.max(0, x0 - pad); y0 = Math.max(0, y0 - pad);
  x1 = Math.min(width - 1, x1 + pad); y1 = Math.min(height - 1, y1 + pad);
  const out = document.createElement('canvas');
  out.width = x1 - x0 + 1;
  out.height = y1 - y0 + 1;
  out.getContext('2d').drawImage(canvas, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
  return out;
}

function canvasToBlob(canvas) {
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
}

function drawPad(color) {
  const wrap = h('div', { class: 'sig-pad' });
  const canvas = h('canvas');
  wrap.append(h('div', { class: 'baseline' }), h('div', { class: 'sig-hint', text: 'Sign above the line' }), canvas);
  let strokes = [];
  let ink = color;
  const dpr = Math.max(2, window.devicePixelRatio || 1);
  const resize = () => {
    const r = wrap.getBoundingClientRect();
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    redraw();
  };
  const redraw = () => {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const s of strokes) {
      ctx.strokeStyle = s.color;
      ctx.beginPath();
      const pts = s.points;
      if (pts.length === 1) {
        ctx.fillStyle = s.color;
        ctx.arc(pts[0][0] * dpr, pts[0][1] * dpr, 1.4 * dpr, 0, Math.PI * 2);
        ctx.fill();
        continue;
      }
      ctx.moveTo(pts[0][0] * dpr, pts[0][1] * dpr);
      for (let i = 1; i < pts.length - 1; i++) {
        const mx = (pts[i][0] + pts[i + 1][0]) / 2;
        const my = (pts[i][1] + pts[i + 1][1]) / 2;
        // thinner when moving fast: approximate pen pressure
        ctx.lineWidth = s.widths[i] * dpr;
        ctx.quadraticCurveTo(pts[i][0] * dpr, pts[i][1] * dpr, mx * dpr, my * dpr);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(mx * dpr, my * dpr);
      }
      const last = pts[pts.length - 1];
      ctx.lineTo(last[0] * dpr, last[1] * dpr);
      ctx.stroke();
    }
    wrap.querySelector('.sig-hint').style.display = strokes.length ? 'none' : '';
  };
  canvas.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    const r = canvas.getBoundingClientRect();
    const stroke = { color: ink, points: [[e.clientX - r.left, e.clientY - r.top]], widths: [2.6], t: performance.now() };
    strokes.push(stroke);
    const move = (ev) => {
      for (const ce of ev.getCoalescedEvents?.() || [ev]) {
        const p = [ce.clientX - r.left, ce.clientY - r.top];
        const prev = stroke.points[stroke.points.length - 1];
        const dist = Math.hypot(p[0] - prev[0], p[1] - prev[1]);
        if (dist < 1) continue;
        const now = performance.now();
        const speed = dist / Math.max(1, now - stroke.t);
        stroke.t = now;
        const target = Math.max(1.3, Math.min(3.4, 3.6 - speed * 1.6));
        const w = stroke.widths[stroke.widths.length - 1] * 0.7 + target * 0.3;
        stroke.points.push(p);
        stroke.widths.push(w);
      }
      redraw();
    };
    const up = () => {
      canvas.removeEventListener('pointermove', move);
      canvas.removeEventListener('pointerup', up);
      redraw();
    };
    canvas.addEventListener('pointermove', move);
    canvas.addEventListener('pointerup', up);
  });
  return {
    el: wrap,
    resize,
    clear: () => { strokes = []; redraw(); },
    setColor: (c) => { ink = c; strokes.forEach((s) => { s.color = c; }); redraw(); },
    isEmpty: () => strokes.length === 0,
    toCanvas: () => trimCanvas(canvas, 6 * dpr),
  };
}

function renderTyped(text, font, color) {
  const canvas = document.createElement('canvas');
  const size = 120;
  const ctx = canvas.getContext('2d');
  ctx.font = `${size}px "${font}", cursive`;
  const w = Math.ceil(ctx.measureText(text).width) + size;
  canvas.width = w;
  canvas.height = Math.ceil(size * 1.9);
  const c2 = canvas.getContext('2d');
  c2.font = `${size}px "${font}", cursive`;
  c2.fillStyle = color;
  c2.textBaseline = 'middle';
  c2.fillText(text, size / 2, canvas.height / 2);
  return trimCanvas(canvas, 10);
}

async function removeWhite(dataUrl) {
  const img = await loadImage(dataUrl);
  const canvas = document.createElement('canvas');
  const scaleDown = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
  canvas.width = Math.round(img.naturalWidth * scaleDown);
  canvas.height = Math.round(img.naturalHeight * scaleDown);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const d = ctx.getImageData(0, 0, canvas.width, canvas.height);
  for (let i = 0; i < d.data.length; i += 4) {
    const lum = 0.299 * d.data[i] + 0.587 * d.data[i + 1] + 0.114 * d.data[i + 2];
    if (lum > 215) d.data[i + 3] = 0;
    else if (lum > 170) d.data[i + 3] = Math.round(d.data[i + 3] * (215 - lum) / 45);
  }
  ctx.putImageData(d, 0, 0);
  return trimCanvas(canvas, 4) || canvas;
}

async function useCanvas(canvas, { save }) {
  // keep uploads reasonable: max 900px wide
  let out = canvas;
  if (canvas.width > 900) {
    out = document.createElement('canvas');
    out.width = 900;
    out.height = Math.round(canvas.height * 900 / canvas.width);
    out.getContext('2d').drawImage(canvas, 0, 0, out.width, out.height);
  }
  const blob = await canvasToBlob(out);
  if (save) {
    const url = out.toDataURL('image/png');
    const list = loadSaved().filter((s) => s !== url);
    storeSaved([url, ...list]);
  }
  const res = await uploadImage(blob);
  beginPlacing({ asset: res.asset, w: res.w, h: res.h, kind: 'signature' });
}

export function openSignatureDialog() {
  if (!S.doc) return;
  let tab = loadSaved().length ? 'saved' : 'draw';
  let color = '#0f172a';
  const tabs = h('div', { class: 'tabs' });
  const content = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px', minHeight: '270px' } });
  const saveCheck = h('input', { type: 'checkbox', checked: true });
  const saveRow = h('label', { class: 'check-row' }, saveCheck, h('span', { text: 'Remember this signature on this computer' }));
  let pad = null;
  let typed = { text: '', font: SCRIPT_FONTS[0] };
  let upload = null;

  const colorRow = () => h('div', { class: 'row' },
    h('span', { class: 'field-label', text: 'Ink' }),
    h('div', { class: 'swatches' }, ['#0f172a', '#1d4ed8', '#b91c1c'].map((c) => {
      const b = h('button', { class: `swatch${c === color ? ' on' : ''}`, style: { background: c } });
      b.addEventListener('click', () => {
        color = c;
        b.parentElement.querySelectorAll('.swatch').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        pad?.setColor(c);
        if (tab === 'type') renderTab();
      });
      return b;
    })));

  const renderTab = () => {
    tabs.replaceChildren(...[['draw', 'Draw'], ['type', 'Type'], ['upload', 'Upload'], ['saved', `Saved (${loadSaved().length})`]].map(([id, label]) => {
      const b = h('button', { class: id === tab ? 'on' : '', text: label });
      b.addEventListener('click', () => { tab = id; renderTab(); });
      return b;
    }));
    content.replaceChildren();
    saveRow.hidden = tab === 'saved';
    if (tab === 'draw') {
      pad = drawPad(color);
      content.append(pad.el, h('div', { class: 'row' }, colorRow(), h('div', { class: 'grow' }), h('button', { class: 'btn ghost', text: 'Clear', onclick: () => pad.clear() })));
      requestAnimationFrame(() => pad.resize());
    } else if (tab === 'type') {
      pad = null;
      const nameInput = h('input', { class: 'input', placeholder: 'Type your name', value: typed.text });
      const grid = h('div', { class: 'type-sigs' });
      const drawOptions = () => {
        grid.replaceChildren(...SCRIPT_FONTS.map((f) => {
          const opt = h('div', { class: `type-sig${f === typed.font ? ' on' : ''}`, text: typed.text || 'Your Name', style: { fontFamily: `"${f}", cursive`, color } });
          opt.addEventListener('click', () => { typed.font = f; drawOptions(); });
          return opt;
        }));
      };
      nameInput.addEventListener('input', () => { typed.text = nameInput.value; drawOptions(); });
      nameInput.addEventListener('keydown', (e) => e.stopPropagation());
      drawOptions();
      content.append(nameInput, grid, colorRow());
      setTimeout(() => nameInput.focus(), 20);
    } else if (tab === 'upload') {
      pad = null;
      const preview = h('div', { class: 'sig-item', style: { height: '140px', cursor: 'default' } });
      const zone = h('label', { class: 'drop-zone-small', html: `${icon('upload')}<div>Choose an image of your signature</div><div style="font-size:12px;color:var(--text-3)">PNG or JPG — a photo of a signature on white paper works well</div>` });
      const fileInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
      zone.append(fileInput);
      const cleanCheck = h('input', { type: 'checkbox', checked: true });
      const process = async () => {
        if (!upload?.dataUrl) return;
        upload.canvas = cleanCheck.checked ? await removeWhite(upload.dataUrl) : await (async () => {
          const img = await loadImage(upload.dataUrl);
          const c = document.createElement('canvas');
          c.width = img.naturalWidth;
          c.height = img.naturalHeight;
          c.getContext('2d').drawImage(img, 0, 0);
          return c;
        })();
        preview.replaceChildren(h('img', { src: upload.canvas.toDataURL('image/png') }));
      };
      fileInput.addEventListener('change', async () => {
        const f = fileInput.files[0];
        if (!f) return;
        upload = { dataUrl: await readAsDataURL(f) };
        await process();
      });
      cleanCheck.addEventListener('change', process);
      content.append(zone, preview, h('label', { class: 'check-row' }, cleanCheck, h('span', { text: 'Remove white background' })));
      if (upload) process();
    } else {
      pad = null;
      const saved = loadSaved();
      if (!saved.length) {
        content.append(h('div', { class: 'hint-box', text: 'No saved signatures yet. Draw, type or upload one — it will be remembered here.' }));
      } else {
        const list = h('div', { class: 'sig-list' });
        saved.forEach((url) => {
          const item = h('div', { class: 'sig-item', 'data-tip': 'Use this signature' }, h('img', { src: url, alt: 'Saved signature' }));
          const del = h('button', { class: 'icon-btn small del', html: icon('trash'), 'data-tip': 'Forget' });
          del.addEventListener('click', (e) => {
            e.stopPropagation();
            storeSaved(loadSaved().filter((s) => s !== url));
            renderTab();
          });
          item.append(del);
          item.addEventListener('click', async () => {
            const img = await loadImage(url);
            const c = document.createElement('canvas');
            c.width = img.naturalWidth;
            c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img, 0, 0);
            ctx.close();
            await useCanvas(c, { save: false });
          });
          list.append(item);
        });
        content.append(list);
      }
    }
  };

  const ctx = openDialog({
    title: 'Add signature',
    wide: true,
    body: [tabs, content, saveRow],
    actions: [
      { label: 'Cancel', kind: 'ghost' },
      { label: 'Use signature', kind: 'primary', icon: 'signature', onClick: async () => {
        let canvas = null;
        if (tab === 'draw') {
          if (!pad || pad.isEmpty()) throw new Error('Draw your signature first.');
          canvas = pad.toCanvas();
        } else if (tab === 'type') {
          if (!typed.text.trim()) throw new Error('Type your name first.');
          await document.fonts.load(`40px "${typed.font}"`).catch(() => {});
          canvas = renderTyped(typed.text.trim(), typed.font, color);
        } else if (tab === 'upload') {
          if (!upload?.canvas) throw new Error('Choose an image first.');
          canvas = upload.canvas;
        } else {
          throw new Error('Click a saved signature to use it.');
        }
        if (!canvas) throw new Error('The signature is empty.');
        await useCanvas(canvas, { save: saveCheck.checked });
      } },
    ],
  });
  renderTab();
}
