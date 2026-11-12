/* 快速切换器 Ctrl+O：模糊片段 → file: 检索 → 键盘导航（P0-8）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $ } from './utils.js';

let overlayEl = null;
let inputEl = null;
let resultsEl = null;
let items = [];
let selected = 0;
let debouncedTimer = null;

export function initSwitcher() {
  overlayEl = $('#overlay-switcher');
  inputEl = $('#switcher-input');
  resultsEl = $('#switcher-results');

  overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) closeSwitcher(); });
  inputEl.addEventListener('input', () => {
    clearTimeout(debouncedTimer);
    debouncedTimer = setTimeout(() => query(inputEl.value), 150);
  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSwitcher(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    if (e.key === 'Enter') { e.preventDefault(); pick(); }
  });
}

export function openSwitcher() {
  overlayEl.classList.remove('hidden');
  inputEl.value = '';
  items = [];
  selected = 0;
  query('');
  inputEl.focus();
}

export function closeSwitcher() {
  overlayEl.classList.add('hidden');
}

async function query(q) {
  try {
    const data = await api.get('/api/search', { q: q ? `file:${q}` : '', limit: 20 });
    items = data.hits;
    selected = 0;
    render();
  } catch (err) { reportError(err); }
}

function render() {
  resultsEl.innerHTML = '';
  if (!items.length) {
    resultsEl.innerHTML = '<div class="panel-empty">无匹配笔记</div>';
    return;
  }
  items.forEach((hit, i) => {
    const el = document.createElement('div');
    el.className = `result-item${i === selected ? ' result-item--active' : ''}`;
    el.dataset.path = hit.path;
    el.innerHTML = `
      <span class="result-item__title">${escapeText(hit.title)}</span>
      <span class="result-item__path">${escapeText(hit.path)}</span>`;
    el.addEventListener('click', () => { emit('open-note', hit.path); closeSwitcher(); });
    resultsEl.appendChild(el);
  });
  const active = resultsEl.querySelector('.result-item--active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function move(delta) {
  if (!items.length) return;
  selected = (selected + delta + items.length) % items.length;
  render();
}

function pick() {
  const hit = items[selected];
  if (hit) { emit('open-note', hit.path); closeSwitcher(); }
}

function escapeText(text) {
  const div = document.createElement('div');
  div.textContent = String(text == null ? '' : text);
  return div.innerHTML;
}
