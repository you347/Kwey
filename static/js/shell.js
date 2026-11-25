/* 界面外壳：ribbon / 左右多 tab 面板 / 可拖拽分隔条 / 状态栏 / 侧栏搜索。
 * 只负责布局与切换；面板内容由各功能模块通过 on('panel-activated') 自订阅填充。 */

import { api } from './api.js';
import { state, on, emit } from './state.js';
import { saveSettings } from './settings/index.js';
import { $, $all, escapeHtml, toast } from './utils.js';

const LEFT_PANELS = ['files', 'search', 'tags', 'bookmarks', 'recent', 'outline', 'trash'];
const RIGHT_PANELS = ['backlinks', 'graph', 'chat'];
const LS_WIDTH = 'kwey.panel-widths.v1';

let active = { left: 'files', right: 'backlinks' };
let initialized = false;

/* ------------------------------------------------------------------ 初始化 */
export function initShell() {
  if (initialized) return;
  initialized = true;

  $all('#ribbon .ribbon__btn[data-panel]').forEach((btn) => {
    btn.addEventListener('click', () => activatePanel(btn.dataset.side, btn.dataset.panel));
  });
  $all('#left-tabs button').forEach((btn) => {
    btn.addEventListener('click', () => activatePanel('left', btn.dataset.panel));
  });
  $all('#right-tabs button').forEach((btn) => {
    btn.addEventListener('click', () => activatePanel('right', btn.dataset.panel));
  });

  $('#sidebar-collapse').addEventListener('click', () => {
    $('#sidebar').classList.toggle('collapsed');
    syncCollapseButtons();
  });
  $('#rightbar-collapse').addEventListener('click', () => {
    $('#rightbar').classList.toggle('collapsed');
    syncCollapseButtons();
  });

  bindResizer($('#left-resizer'), 'left');
  bindResizer($('#right-resizer'), 'right');
  bindPanelSearch();
  initRibbonTips();

  applyWidths(readWidths());
  activatePanel('left', active.left, false);
  activatePanel('right', active.right, false);
}

/** vault 就绪后按 settings 恢复面板与宽度（state.settings 此时已加载）。 */
export function onVaultReady() {
  const s = state.settings || {};
  applyWidths({
    left: Number(s.sidebar_width) || readWidths().left,
    right: Number(s.rightbar_width) || readWidths().right,
  });
  activatePanel('left', LEFT_PANELS.includes(s.left_panel) ? s.left_panel : active.left, false);
  activatePanel('right', RIGHT_PANELS.includes(s.right_panel) ? s.right_panel : active.right, false);
}

/* ------------------------------------------------------------------ ribbon 短标签 */
/** 悬浮/键盘聚焦时显示按钮的 data-tip 短名称；点击行为不变。 */
function initRibbonTips() {
  const ribbon = $('#ribbon');
  const tip = $('#ribbon-tip');
  if (!ribbon || !tip) return;

  let showTimer = null;
  let currentBtn = null;

  const hide = () => {
    clearTimeout(showTimer);
    showTimer = null;
    currentBtn = null;
    tip.classList.remove('is-visible');
    tip.setAttribute('aria-hidden', 'true');
  };

  const place = (btn) => {
    const rect = btn.getBoundingClientRect();
    const box = tip.getBoundingClientRect();
    const pad = 8;
    let left = rect.right + pad;
    let top = rect.top + (rect.height - box.height) / 2;
    if (left + box.width > window.innerWidth - pad) {
      left = rect.left - box.width - pad;
    }
    left = Math.max(pad, Math.min(left, window.innerWidth - box.width - pad));
    top = Math.max(pad, Math.min(top, window.innerHeight - box.height - pad));
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  };

  const show = (btn) => {
    if (!btn) return;
    currentBtn = btn;
    clearTimeout(showTimer);
    showTimer = setTimeout(() => {
      if (currentBtn !== btn) return;
      const label = btn.dataset.tip || btn.getAttribute('aria-label') || '';
      if (!label) return;
      tip.textContent = label;
      tip.style.visibility = 'hidden';
      tip.classList.add('is-visible');
      place(btn);
      tip.style.visibility = '';
      tip.setAttribute('aria-hidden', 'false');
    }, 90);
  };

  ribbon.addEventListener('pointerover', (e) => {
    const btn = e.target.closest('.ribbon__btn');
    if (!btn) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;
    show(btn);
  });
  ribbon.addEventListener('pointerout', (e) => {
    const btn = e.target.closest('.ribbon__btn');
    if (!btn) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;
    if (currentBtn === btn) hide();
  });
  ribbon.addEventListener('pointerdown', hide, true);
  ribbon.addEventListener('focusin', (e) => {
    const btn = e.target.closest('.ribbon__btn');
    // 鼠标点击后的聚焦不弹标签；键盘 Tab 进入时 :focus-visible 生效。
    if (btn && (!btn.matches || btn.matches(':focus-visible'))) show(btn);
  });
  ribbon.addEventListener('focusout', (e) => {
    const btn = e.target.closest('.ribbon__btn');
    if (btn && currentBtn === btn) hide();
  });
  window.addEventListener('blur', hide);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
}

/* ------------------------------------------------------------------ 面板切换 */
export function activatePanel(side, name, persist = true) {
  const list = side === 'left' ? LEFT_PANELS : RIGHT_PANELS;
  if (!list.includes(name)) return;
  active[side] = name;

  const bar = $(side === 'left' ? '#sidebar' : '#rightbar');
  const tabs = $(side === 'left' ? '#left-tabs' : '#right-tabs');
  if (side === 'left') bar.classList.remove('collapsed');
  else bar.classList.remove('collapsed');

  bar.querySelectorAll('.panel-view').forEach((view) => {
    view.classList.toggle('hidden', view.dataset.panel !== name);
  });
  tabs.querySelectorAll('button').forEach((btn) => {
    btn.classList.toggle('panel-tab--active', btn.dataset.panel === name);
  });
  $all('#ribbon .ribbon__btn[data-panel]').forEach((btn) => {
    if (btn.dataset.side !== side) return;
    btn.classList.toggle('ribbon__btn--active', btn.dataset.panel === name);
  });

  if (persist) {
    const key = side === 'left' ? 'left_panel' : 'right_panel';
    state.settings[key] = name;
    if (Object.keys(state.settings).length) saveSettings({ [key]: name });
  }
  emit('panel-activated', { side, name });
}

export function currentPanel(side) { return active[side]; }

/* ------------------------------------------------------------------ 分隔条 */
function readWidths() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(LS_WIDTH) || '{}'); } catch { /* 忽略损坏值 */ }
  return {
    left: Number(saved.left) || Number(state.settings?.sidebar_width) || 240,
    right: Number(saved.right) || Number(state.settings?.rightbar_width) || 260,
  };
}

function applyWidths(widths) {
  document.documentElement.style.setProperty('--sidebar-width', `${widths.left}px`);
  document.documentElement.style.setProperty('--rightbar-width', `${widths.right}px`);
}

function bindResizer(el, side) {
  if (!el) return;
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const bar = $(side === 'left' ? '#sidebar' : '#rightbar');
    bar.classList.remove('collapsed');
    const startX = e.clientX;
    const startW = bar.getBoundingClientRect().width;
    const key = side === 'left' ? '--sidebar-width' : '--rightbar-width';
    el.classList.add('is-dragging');
    document.body.classList.add('is-resizing');
    try { el.setPointerCapture(e.pointerId); } catch { /* 某些 WebView 不支持 */ }

    const onMove = (ev) => {
      const delta = ev.clientX - startX;
      const w = Math.round(Math.max(160, Math.min(560, side === 'left' ? startW + delta : startW - delta)));
      document.documentElement.style.setProperty(key, `${w}px`);
    };
    const onUp = () => {
      el.removeEventListener('pointermove', onMove);
      el.classList.remove('is-dragging');
      document.body.classList.remove('is-resizing');
      const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue(key), 10) || startW;
      const widths = readWidths();
      widths[side] = w;
      try { localStorage.setItem(LS_WIDTH, JSON.stringify(widths)); } catch { /* 忽略 */ }
      const field = side === 'left' ? 'sidebar_width' : 'rightbar_width';
      state.settings[field] = w;
      if (Object.keys(state.settings).length > 1) saveSettings({ [field]: w });
      emit('panel-resized', { side, width: w });
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp, { once: true });
    el.addEventListener('pointercancel', onUp, { once: true });
  });
}

/* ------------------------------------------------------------------ 侧栏搜索 */
function bindPanelSearch() {
  const input = $('#panel-search-input');
  const results = $('#panel-search-results');
  if (!input || !results) return;
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => runPanelSearch(input.value, results), 180);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const first = results.querySelector('.result-item');
    if (first && first.dataset.path) emit('open-note', first.dataset.path);
  });
  on('panel-activated', ({ side, name }) => {
    if (side === 'left' && name === 'search') input.focus();
  });
}

async function runPanelSearch(q, results) {
  if (!q.trim()) {
    results.innerHTML = '<div class="panel-empty">输入关键词，支持 path: tag: file: 操作符</div>';
    return;
  }
  try {
    const data = await api.get('/api/search', { q, limit: 50 });
    if (!data.hits.length) {
      results.innerHTML = '<div class="panel-empty">无匹配笔记</div>';
      return;
    }
    results.innerHTML = data.hits.map((hit) => `
      <div class="result-item" data-path="${escapeHtml(hit.path)}">
        <span class="result-item__title">${escapeHtml(hit.title)}</span>
        <span class="result-item__path">${escapeHtml(hit.path)}</span>
        ${hit.snippet ? `<span class="result-item__snippet">${hit.snippet}</span>` : ''}
      </div>`).join('');
    results.querySelectorAll('.result-item').forEach((el) => {
      el.addEventListener('click', () => emit('open-note', el.dataset.path));
    });
  } catch (err) {
    results.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 状态栏 */
export function setStatusCursor(pos) {
  const el = $('#status-cursor');
  if (el && pos) el.textContent = `行 ${pos.line} · 列 ${pos.ch}`;
  const sel = $('#status-selection');
  if (sel) sel.textContent = pos && pos.selLen > 0 ? `已选 ${pos.selLen} 字符` : '';
}

export function setStatusPath(path) {
  const el = $('#status-path');
  if (el) el.textContent = path || '';
}

/* ------------------------------------------------------------------ 专注模式 */
let focusSnapshot = null;

/** 隐藏/恢复两侧面板（命令面板入口）；退出时还原进入前的折叠状态。 */
export function toggleFocusMode() {
  const sb = $('#sidebar');
  const rb = $('#rightbar');
  if (focusSnapshot) {
    sb.classList.toggle('collapsed', focusSnapshot.left);
    rb.classList.toggle('collapsed', focusSnapshot.right);
    focusSnapshot = null;
    toast('已退出专注模式', 'info', 1500);
  } else {
    focusSnapshot = {
      left: sb.classList.contains('collapsed'),
      right: rb.classList.contains('collapsed'),
    };
    sb.classList.add('collapsed');
    rb.classList.add('collapsed');
    toast('专注模式：再次执行「专注模式」命令退出', 'info', 2400);
  }
  syncCollapseButtons();
  emit('focus-mode-changed', { on: !!focusSnapshot });
}

function syncCollapseButtons() {
  $('#sidebar-collapse').classList.toggle('ribbon__btn--active', $('#sidebar').classList.contains('collapsed'));
  $('#rightbar-collapse').classList.toggle('ribbon__btn--active', $('#rightbar').classList.contains('collapsed'));
}
