/* 模板：模板选择浮层 + 每日笔记（P1）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, fuzzyScore, toast, basename, stripMd } from './utils.js';

let initialized = false;
let overlayEl = null;
let inputEl = null;
let listEl = null;
let items = [];        // 全量模板 [{name,path}]
let filtered = [];     // 当前过滤结果
let selected = 0;

/** 初始化一次：绑定浮层、输入过滤 / 键盘导航、结果列表点击。 */
export function initTemplates() {
  if (initialized) return;
  initialized = true;
  overlayEl = $('#overlay-templates');
  inputEl = $('#template-input');
  listEl = $('#template-list');
  if (!overlayEl || !inputEl || !listEl) return;  // DOM 缺失则安全跳过

  overlayEl.addEventListener('mousedown', (e) => {
    if (e.target === overlayEl) closeTemplates();
  });

  inputEl.addEventListener('input', () => {
    selected = 0;
    render(inputEl.value);
  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeTemplates(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
    if (e.key === 'Enter') { e.preventDefault(); pick(); }
  });

  // 结果项用事件委托，避免每次渲染重复绑定
  listEl.addEventListener('click', (e) => {
    const item = e.target.closest && e.target.closest('.result-item');
    if (!item || !listEl.contains(item)) return;
    const idx = Number(item.dataset.index);
    if (Number.isNaN(idx)) return;
    selected = idx;
    pick();
  });
}

/** 打开模板浮层并拉取模板列表。 */
export function openTemplates() {
  initTemplates();
  if (!overlayEl || !inputEl || !listEl) return;
  overlayEl.classList.remove('hidden');
  inputEl.value = '';
  items = [];
  filtered = [];
  selected = 0;
  render('');
  inputEl.focus();
  loadTemplates();
}

function closeTemplates() {
  if (overlayEl) overlayEl.classList.add('hidden');
}

async function loadTemplates() {
  try {
    const data = await api.get('/api/templates');
    items = Array.isArray(data) ? data : [];
    selected = 0;
    render(inputEl ? inputEl.value : '');
  } catch (err) {
    reportError(err);
  }
}

/** 依据输入做模糊过滤并渲染 .result-item 列表。 */
function render(query) {
  if (!listEl) return;
  const q = (query || '').trim();
  filtered = items
    .map((t) => ({ t, score: fuzzyScore(q, t.name) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.t);
  if (selected >= filtered.length) selected = 0;

  listEl.innerHTML = '';
  if (!filtered.length) {
    listEl.innerHTML = '<div class="panel-empty">暂无模板（放在 templates/ 目录下的 .md 文件）</div>';
    return;
  }
  filtered.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = `result-item${i === selected ? ' result-item--active' : ''}`;
    el.dataset.path = t.path;
    el.dataset.index = String(i);
    el.innerHTML = `
      <span class="result-item__title">${escapeHtml(t.name)}</span>
      <span class="result-item__path">${escapeHtml(t.path)}</span>`;
    listEl.appendChild(el);
  });
  const active = listEl.querySelector('.result-item--active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function move(delta) {
  if (!filtered.length) return;
  selected = (selected + delta + filtered.length) % filtered.length;
  render(inputEl ? inputEl.value : '');
}

function pick() {
  const t = filtered[selected];
  if (t) applyTemplate(t.path);
}

/** 套用模板：后端生成笔记后刷新文件树并打开。 */
export async function applyTemplate(templatePath) {
  if (!templatePath) return;
  try {
    const res = await api.post('/api/templates/apply', { template_path: templatePath });
    closeTemplates();
    emit('tree-refresh');
    emit('open-note', res.path);
    toast(`已套用模板：${res.title || stripMd(basename(res.path))}`, 'success');
  } catch (err) {
    reportError(err);
  }
}

/** 打开/创建今日笔记：daily_dir 可在 settings 配置，默认 daily/。 */
export async function openDailyNote() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const rawDir = (state.settings && state.settings.daily_dir) || 'daily';
  const dir = String(rawDir).replace(/^\/+|\/+$/g, '');
  const path = dir ? `${dir}/${date}.md` : `${date}.md`;
  try {
    // 先试探是否已存在；404 是「不存在」的正常信号，不弹 toast
    await api.get('/api/note', { path });
    emit('open-note', path);
  } catch (err) {
    if (err.code !== 404) { reportError(err); return; }
    try {
      // POST /api/note 会自动创建缺失的父目录（VaultService mkdir parents）
      const res = await api.post('/api/note', { path, content: `# ${date}\n\n` });
      emit('tree-refresh');
      emit('open-note', (res && res.path) || path);
    } catch (createErr) {
      reportError(createErr);
    }
  }
}

/** 可选别名：指向 openTemplates。 */
export function insertTemplate() {
  return openTemplates();
}
