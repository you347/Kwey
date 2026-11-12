/* 命令面板 Ctrl+P：命令注册表 + 模糊匹配 + 快捷键提示（P0-9）。 */

import { fuzzyScore, $ } from './utils.js';

const registry = [];
let overlayEl = null;
let inputEl = null;
let resultsEl = null;
let filtered = [];
let selected = 0;

/** 注册命令：{id, title, hotkey?, run}。 */
export function registerCommand(cmd) {
  registry.push(cmd);
}

export function initPalette() {
  overlayEl = $('#overlay-palette');
  inputEl = $('#palette-input');
  resultsEl = $('#palette-results');

  overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) closePalette(); });
  inputEl.addEventListener('input', () => render(inputEl.value));
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closePalette(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    if (e.key === 'Enter') { e.preventDefault(); pick(); }
  });
}

export function openPalette() {
  overlayEl.classList.remove('hidden');
  inputEl.value = '';
  selected = 0;
  render('');
  inputEl.focus();
}

export function closePalette() {
  overlayEl.classList.add('hidden');
}

export function getCommands() {
  return registry.slice();
}

function render(query) {
  filtered = registry
    .map((cmd) => ({ cmd, score: fuzzyScore(query, cmd.title) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.cmd);
  if (selected >= filtered.length) selected = 0;
  resultsEl.innerHTML = '';
  if (!filtered.length) {
    resultsEl.innerHTML = '<div class="panel-empty">无匹配命令</div>';
    return;
  }
  filtered.forEach((cmd, i) => {
    const el = document.createElement('div');
    el.className = `result-item${i === selected ? ' result-item--active' : ''}`;
    el.innerHTML = `
      <span class="result-item__title">${escapeText(cmd.title)}
        ${cmd.hotkey ? `<span class="result-item__hotkey">${escapeText(cmd.hotkey)}</span>` : ''}
      </span>`;
    el.addEventListener('click', () => { run(cmd); });
    resultsEl.appendChild(el);
  });
  const active = resultsEl.querySelector('.result-item--active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function move(delta) {
  if (!filtered.length) return;
  selected = (selected + delta + filtered.length) % filtered.length;
  render(inputEl.value);
}

function pick() {
  const cmd = filtered[selected];
  if (cmd) run(cmd);
}

function run(cmd) {
  closePalette();
  try { cmd.run(); } catch (err) { console.error('[kwey] 命令执行失败', err); }
}

function escapeText(text) {
  const div = document.createElement('div');
  div.textContent = String(text == null ? '' : text);
  return div.innerHTML;
}
