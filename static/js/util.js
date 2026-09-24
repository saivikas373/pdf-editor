import { icon } from './icons.js';

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const mod = (e) => (isMac ? e.metaKey : e.ctrlKey);
export const uid = () => Math.random().toString(36).slice(2, 9) + Date.now().toString(36).slice(-3);

/** Tiny element builder: h('div', {class: 'x', onclick}, child, ...) */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'icon') el.insertAdjacentHTML('afterbegin', icon(v));
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function debounce(fn, ms) {
  let t;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.flush = (...args) => {
    clearTimeout(t);
    fn(...args);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

export function rafThrottle(fn) {
  let scheduled = false;
  let lastArgs;
  return (...args) => {
    lastArgs = args;
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      fn(...lastArgs);
    });
  };
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function pickFiles({ accept = '', multiple = false } = {}) {
  return new Promise((resolve) => {
    const input = h('input', { type: 'file', accept, multiple, style: { display: 'none' } });
    input.addEventListener('change', () => {
      resolve([...input.files]);
      input.remove();
    });
    document.body.append(input);
    input.click();
  });
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: filename, style: { display: 'none' } });
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
    a.remove();
  }, 4000);
}

export function toast(message, { type = 'info', action, actionLabel, timeout } = {}) {
  const root = $('#toasts');
  const iconName = type === 'error' ? 'alert' : type === 'success' ? 'check-circle' : null;
  const el = h('div', { class: `toast ${type}` }, iconName ? h('span', { html: icon(iconName) }) : null, h('span', { text: message }));
  if (action) {
    el.append(h('button', { text: actionLabel || 'Undo', onclick: () => { action(); dismiss(); } }));
  }
  root.append(el);
  const dismiss = () => {
    el.style.transition = 'opacity .2s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 200);
  };
  setTimeout(dismiss, timeout ?? (type === 'error' ? 6500 : 3500));
  while (root.children.length > 4) root.firstElementChild.remove();
  return dismiss;
}

let loadingTimer;
export function setLoading(text) {
  const el = $('#loading');
  clearTimeout(loadingTimer);
  if (!text) {
    el.hidden = true;
    return;
  }
  $('#loading-text').textContent = text;
  // avoid flashing the overlay for quick operations
  loadingTimer = setTimeout(() => { el.hidden = false; }, 250);
}

export function hexToRgb(hex) {
  let v = (hex || '#000000').replace('#', '');
  if (v.length === 3) v = v.split('').map((c) => c + c).join('');
  const n = parseInt(v, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map((v) => clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0')).join('');
}

export function rgba(hex, alpha) {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ---------------------------------------------------------------- tooltips
export function initTooltips() {
  const tip = $('#tooltip');
  let current = null;
  let timer;
  document.addEventListener('pointerover', (e) => {
    const target = e.target.closest?.('[data-tip]');
    if (target === current) return;
    current = target;
    clearTimeout(timer);
    tip.hidden = true;
    if (!target) return;
    timer = setTimeout(() => {
      if (!document.body.contains(target)) return;
      tip.textContent = target.dataset.tip;
      tip.hidden = false;
      const r = target.getBoundingClientRect();
      const tr = tip.getBoundingClientRect();
      let left = r.left + r.width / 2 - tr.width / 2;
      left = clamp(left, 6, window.innerWidth - tr.width - 6);
      let top = r.bottom + 6;
      if (top + tr.height > window.innerHeight - 6) top = r.top - tr.height - 6;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    }, 450);
  });
  document.addEventListener('pointerdown', () => {
    clearTimeout(timer);
    tip.hidden = true;
  });
}

// ---------------------------------------------------------------- menus
let openMenuEl = null;
export function closeMenu() {
  if (openMenuEl) {
    openMenuEl.remove();
    openMenuEl = null;
  }
}

/** items: [{label, icon, shortcut, onClick, disabled, checked}] | '-' | {header} */
export function showMenu(anchor, items, { align = 'left', x, y } = {}) {
  closeMenu();
  const menu = h('div', { class: 'menu', role: 'menu' });
  for (const item of items) {
    if (!item) continue;
    if (item === '-') {
      menu.append(h('div', { class: 'menu-sep' }));
    } else if (item.header) {
      menu.append(h('div', { class: 'menu-label', text: item.header }));
    } else {
      const btn = h('button', { class: 'menu-item', role: 'menuitem', disabled: item.disabled },
        item.icon ? h('span', { html: icon(item.checked === false ? 'x' : item.icon) }) : null,
        h('span', { text: item.label }),
        item.shortcut ? h('span', { class: 'shortcut', text: item.shortcut }) : null);
      btn.addEventListener('click', () => {
        closeMenu();
        item.onClick?.();
      });
      menu.append(btn);
    }
  }
  $('#menus').append(menu);
  openMenuEl = menu;
  const mr = menu.getBoundingClientRect();
  let left;
  let top;
  if (anchor) {
    const r = anchor.getBoundingClientRect();
    left = align === 'right' ? r.right - mr.width : r.left;
    top = r.bottom + 4;
    if (top + mr.height > window.innerHeight - 8) top = Math.max(8, r.top - mr.height - 4);
  } else {
    left = x;
    top = y;
  }
  menu.style.left = `${clamp(left, 8, window.innerWidth - mr.width - 8)}px`;
  menu.style.top = `${clamp(top, 8, window.innerHeight - mr.height - 8)}px`;
  setTimeout(() => {
    const away = (e) => {
      if (!menu.contains(e.target)) {
        closeMenu();
        document.removeEventListener('pointerdown', away, true);
      }
    };
    document.addEventListener('pointerdown', away, true);
  });
  return menu;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function readAsDataURL(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = reject;
    r.readAsDataURL(blob);
  });
}

export function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}
